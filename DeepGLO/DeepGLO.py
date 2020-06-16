from __future__ import print_function
import torch, h5py
import numpy as np
from scipy.io import loadmat
from torch.nn.utils import weight_norm

import torch.nn as nn
import torch.optim as optim
import numpy as np

# import matplotlib
from torch.autograd import Variable
import sys

import itertools
import torch.nn.functional as F
import copy
import os
import gc

from DeepGLO.data_loader import *

from sklearn.decomposition import NMF

use_cuda = False  #### Assuming you have a GPU ######

from DeepGLO.utilities import *


from DeepGLO.LocalModel import *

from DeepGLO.metrics import *

import copy

import random

import pickle

np.random.seed(111)
torch.cuda.manual_seed(111)
torch.manual_seed(111)
random.seed(111)


def get_model(A, y, lamb=0):
    """
    Regularized least-squares
    """
    n_col = A.shape[1]
    return np.linalg.lstsq(
        A.T.dot(A) + lamb * np.identity(n_col), A.T.dot(y), rcond=None
    )


class DeepGLO(object):
    def __init__(
        self,
        Ymat,
        vbsize=150,
        hbsize=256,
        num_channels_X=[32, 32, 32, 32, 1],
        num_channels_Y=[32, 32, 32, 32, 1],
        kernel_size=7,
        dropout=0.2,
        rank=64,
        kernel_size_Y=7,
        lr=0.0005,
        val_len=24,
        end_index=20000,
        normalize=False,
        start_date="2016-1-1",
        freq="H",
        covariates=None,
        use_time=True,
        dti=None,
        svd=False,
        period=None,
        forward_cov=False,
    ):
        self.start_date = start_date
        self.freq = freq
        self.covariates = covariates
        self.use_time = use_time
        self.dti = dti
        self.dropout = dropout
        self.period = period
        self.forward_cov = forward_cov
        self.Xseq = TemporalConvNet(
            num_inputs=1,
            num_channels=num_channels_X,
            kernel_size=kernel_size,
            dropout=dropout,
            init=True,
        )

        if normalize:
            self.s = np.std(Ymat[:, 0:end_index], axis=1)
            # self.s[self.s == 0] = 1.0
            self.s += 1.0
            self.m = np.mean(Ymat[:, 0:end_index], axis=1)
            self.Ymat = (Ymat - self.m[:, None]) / self.s[:, None]
            self.mini = np.abs(np.min(self.Ymat))
            self.Ymat = self.Ymat + self.mini
        else:
            self.Ymat = Ymat
        self.normalize = normalize
        n, T = self.Ymat.shape
        t0 = end_index + 1
        if t0 > T:
            self.Ymat = np.hstack([self.Ymat, self.Ymat[:, -1].reshape(-1, 1)])
        if svd:
            indices = np.random.choice(self.Ymat.shape[0], rank, replace=False)
            X = self.Ymat[indices, 0:t0]
            mX = np.std(X, axis=1)
            mX[mX == 0] = 1.0
            X = X / mX[:, None]
            Ft = get_model(X.transpose(), self.Ymat[:, 0:t0].transpose(), lamb=0.1)
            F = Ft[0].transpose()
            self.X = torch.from_numpy(X).float()
            self.F = torch.from_numpy(F).float()
        else:
            R = torch.zeros(rank, t0).float()
            X = torch.normal(R, 0.1)
            C = torch.zeros(n, rank).float()
            F = torch.normal(C, 0.1)
            self.X = X.float()
            self.F = F.float()
        self.svd = svd
        self.vbsize = vbsize
        self.hbsize = hbsize
        self.num_channels_X = num_channels_X
        self.num_channels_Y = num_channels_Y
        self.kernel_size_Y = kernel_size_Y
        self.rank = rank
        self.kernel_size = kernel_size
        self.lr = lr
        self.val_len = val_len
        self.start_index = 0
        self.end_index = end_index
        self.num_epochs = 0
        self.D = data_loader(
            Ymat=self.Ymat,
            vbsize=vbsize,
            hbsize=hbsize,
            end_index=end_index,
            val_len=val_len,
            shuffle=False,
        )

    def tensor2d_to_temporal(self, T):
        T = T.view(1, T.size(0), T.size(1))
        T = T.transpose(0, 1)
        return T

    def temporal_to_tensor2d(self, T):
        T = T.view(T.size(0), T.size(2))
        return T

    def calculate_newX_loss_vanilla(self, Xn, Fn, Yn, Xf, alpha):
        Yout = torch.mm(Fn, Xn)
        cr1 = nn.L1Loss()
        cr2 = nn.MSELoss()
        l1 = cr2(Yout, Yn) / torch.mean(Yn ** 2)
        l2 = cr2(Xn, Xf) / torch.mean(Xf ** 2)
        return (1 - alpha) * l1 + alpha * l2

    def recover_future_X(
        self,
        last_step,
        future,
        cpu=True,
        num_epochs=50,
        alpha=0.5,
        vanilla=True,
        tol=1e-7,
    ):
        rg = max(
            1 + 2 * (self.kernel_size - 1) * 2 ** (len(self.num_channels_X) - 1),
            1 + 2 * (self.kernel_size_Y - 1) * 2 ** (len(self.num_channels_Y) - 1),
        )
        X = self.X[:, last_step - rg : last_step]
        X = self.tensor2d_to_temporal(X)
        outX = self.predict_future(model=self.Xseq, inp=X, future=future, cpu=cpu)
        outX = self.temporal_to_tensor2d(outX)
        Xf = outX[:, -future::]
        Yn = self.Ymat[:, last_step : last_step + future]
        Yn = torch.from_numpy(Yn).float()
        cpu = True
        if cpu:
            self.Xseq = self.Xseq.cpu()
        else:
            Yn = Yn.cuda()
            Xf = Xf.cuda()

        Fn = self.F

        Xt = torch.zeros(self.rank, future).float()
        Xn = torch.normal(Xt, 0.1)
        cpu = True
        if not cpu:
            Xn = Xn.cuda()
        lprev = 0
        for i in range(num_epochs):
            Xn = Variable(Xn, requires_grad=True)
            optim_Xn = optim.Adam(params=[Xn], lr=self.lr)
            optim_Xn.zero_grad()
            loss = self.calculate_newX_loss_vanilla(
                Xn, Fn.detach(), Yn.detach(), Xf.detach(), alpha
            )
            loss.backward()
            optim_Xn.step()
            # Xn = torch.clamp(Xn.detach(), min=0)

            if np.abs(lprev - loss.cpu().item()) <= tol:
                break

            if i % 1000 == 0:
                print("Recovery Loss: " + str(loss.cpu().item()))
                lprev = loss.cpu().item()

        # self.Xseq = self.Xseq.cuda()

        return Xn.detach()

    def step_factX_loss(self, inp, out, last_vindex, last_hindex, reg=0.0):
        Xout = self.X[:, last_hindex + 1 : last_hindex + 1 + out.size(2)]
        Fout = self.F[self.D.I[last_vindex : last_vindex + out.size(0)], :]
        use_cuda = False
        if use_cuda:
            Xout = Xout.cuda()
            Fout = Fout.cuda()
        Xout = Variable(Xout, requires_grad=True)
        out = self.temporal_to_tensor2d(out)
        optim_X = optim.Adam(params=[Xout], lr=self.lr)
        Hout = torch.matmul(Fout, Xout)
        optim_X.zero_grad()
        loss = torch.mean(torch.pow(Hout - out.detach(), 2))
        l2 = torch.mean(torch.pow(Xout, 2))
        r = loss.detach() / l2.detach()
        loss = loss + r * reg * l2
        loss.backward()
        optim_X.step()
        # Xout = torch.clamp(Xout, min=0)
        self.X[:, last_hindex + 1 : last_hindex + 1 + inp.size(2)] = Xout.cpu().detach()
        return loss

    def step_factF_loss(self, inp, out, last_vindex, last_hindex, reg=0.0):
        Xout = self.X[:, last_hindex + 1 : last_hindex + 1 + out.size(2)]
        Fout = self.F[self.D.I[last_vindex : last_vindex + out.size(0)], :]
        use_cuda = False
        if use_cuda:
            Xout = Xout.cuda()
            Fout = Fout.cuda()
        Fout = Variable(Fout, requires_grad=True)
        optim_F = optim.Adam(params=[Fout], lr=self.lr)
        out = self.temporal_to_tensor2d(out)
        Hout = torch.matmul(Fout, Xout)
        optim_F.zero_grad()
        loss = torch.mean(torch.pow(Hout - out.detach(), 2))
        l2 = torch.mean(torch.pow(Fout, 2))
        r = loss.detach() / l2.detach()
        loss = loss + r * reg * l2
        loss.backward()
        optim_F.step()
        self.F[
            self.D.I[last_vindex : last_vindex + inp.size(0)], :
        ] = Fout.cpu().detach()
        return loss

    def step_temporal_loss_X(self, inp, last_vindex, last_hindex):
        Xin = self.X[:, last_hindex : last_hindex + inp.size(2)]
        Xout = self.X[:, last_hindex + 1 : last_hindex + 1 + inp.size(2)]
        for p in self.Xseq.parameters():
            p.requires_grad = False
        use_cuda = False
        if use_cuda:
            Xin = Xin.cuda()
            Xout = Xout.cuda()
        Xin = Variable(Xin, requires_grad=True)
        Xout = Variable(Xout, requires_grad=True)
        optim_out = optim.Adam(params=[Xout], lr=self.lr)
        Xin = self.tensor2d_to_temporal(Xin)
        Xout = self.tensor2d_to_temporal(Xout)
        hatX = self.Xseq(Xin)
        optim_out.zero_grad()
        loss = torch.mean(torch.pow(Xout - hatX.detach(), 2))
        loss.backward()
        optim_out.step()
        # Xout = torch.clamp(Xout, min=0)
        temp = self.temporal_to_tensor2d(Xout.detach())
        self.X[:, last_hindex + 1 : last_hindex + 1 + inp.size(2)] = temp
        return loss

    def predict_future_batch(self, model, inp, future=10, cpu=True):
        cpu = True
        if cpu:
            model = model.cpu()
            inp = inp.cpu()
        else:
            inp = inp.cuda()

        out = model(inp)
        output = out[:, :, out.size(2) - 1].view(out.size(0), out.size(1), 1)
        out = torch.cat((inp, output), dim=2)
        torch.cuda.empty_cache()
        for i in range(future - 1):
            inp = out
            out = model(inp)
            output = out[:, :, out.size(2) - 1].view(out.size(0), out.size(1), 1)
            out = torch.cat((inp, output), dim=2)
            torch.cuda.empty_cache()

        out = self.temporal_to_tensor2d(out)
        out = np.array(out.cpu().detach())
        return out

    def predict_future(self, model, inp, future=10, cpu=True, bsize=90):
        n = inp.size(0)
        inp = inp.cpu()
        ids = np.arange(0, n, bsize)
        ids = list(ids) + [n]
        out = self.predict_future_batch(model, inp[ids[0] : ids[1], :, :], future, cpu)
        torch.cuda.empty_cache()

        for i in range(1, len(ids) - 1):
            temp = self.predict_future_batch(
                model, inp[ids[i] : ids[i + 1], :, :], future, cpu
            )
            torch.cuda.empty_cache()
            out = np.vstack([out, temp])

        out = torch.from_numpy(out).float()
        return self.tensor2d_to_temporal(out)

    def predict_global(
        self, ind, last_step=100, future=10, cpu=False, normalize=False, bsize=90
    ):

        if ind is None:
            ind = np.arange(self.Ymat.shape[0])
        if cpu:
            self.Xseq = self.Xseq.cpu()

        self.Xseq = self.Xseq.eval()

        rg = max(
            1 + 2 * (self.kernel_size - 1) * 2 ** (len(self.num_channels_X) - 1),
            1 + 2 * (self.kernel_size_Y - 1) * 2 ** (len(self.num_channels_Y) - 1),
        )
        X = self.X[:, last_step - rg : last_step]
        n = X.size(0)
        T = X.size(1)
        X = self.tensor2d_to_temporal(X)
        outX = self.predict_future(
            model=self.Xseq, inp=X, future=future, cpu=cpu, bsize=bsize
        )

        outX = self.temporal_to_tensor2d(outX)

        F = self.F

        Y = torch.matmul(F, outX)

        Y = np.array(Y[ind, :].cpu().detach())

        # self.Xseq = self.Xseq.cuda()

        del F

        torch.cuda.empty_cache()

        for p in self.Xseq.parameters():
            p.requires_grad = True

        if normalize:
            Y = Y - self.mini
            Y = Y * self.s[ind, None] + self.m[ind, None]
            return Y
        else:
            return Y

    def prepare_increfit(self,
                     Ymat_incr
                     ):
        # prepare internal data structures

        # normalize the incremented Ymat if needed
        if self.normalize:
            # TODO check the correctness of this part
            # self.s = np.std(Ymat[:, 0:end_index], axis=1)
            # self.s[self.s == 0] = 1.0
            # self.s += 1.0
            # self.m = np.mean(Ymat[:, 0:end_index], axis=1)
            Ymat_incr = (Ymat_incr - self.m[:, None]) / self.s[:, None]
            # self.mini = np.abs(np.min(self.Ymat))
            Ymat_incr = Ymat_incr + self.mini
        else:
            pass

        # append the new Ymat onto the original,
        # reset start/end index and Ymat in D
        if self.Ymat.shape[0] != Ymat_incr.shape[0]:
            raise RuntimeError("incremented no. of time series should have the same dimension as original")
        n, T_incr = Ymat_incr.shape
        # TODO how to deal with the Ymat data after end_index?
        self.Ymat = np.concatenate((self.Ymat[:, : self.end_index], Ymat_incr), axis=1)
        self.D.start_index = self.end_index
        self.end_index = self.end_index + T_incr
        self.D.end_index = self.end_index
        self.start_index = self.D.start_index


        # initialize the newly added X
        n, T = self.Ymat.shape
        t0 = self.end_index + 1
        if t0 > T:
            self.Ymat = np.hstack([self.Ymat, self.Ymat[:, -1].reshape(-1, 1)])
        if self.svd:
            # TODO whether it is correct to initialize new X this way?
            indices = np.random.choice(self.Ymat.shape[0], self.rank, replace=False)
            X = self.Ymat[indices, 0:t0]
            mX = np.std(X, axis=1)
            mX[mX == 0] = 1.0
            X = X / mX[:, None]
            # only append the last few X to X
            Xn = torch.from_numpy(X[:, self.D.start_index:t0]).float()
            self.X = torch.cat([self.X[:, :self.D.start_index], Xn], dim=1)
            # TODO: do not refit F for now. shall we?
            # Ft = get_model(X.transpose(), self.Ymat[:, 0:t0].transpose(), lamb=0.1)
            # F = Ft[0].transpose()
            # self.F = torch.from_numpy(F).float()
        else:
            R = torch.zeros(rank, t0).float()
            X = torch.normal(R, 0.1).float()
            Xn = X[:, self.D.start_index:t0]
            self.X = torch.cat([self.X, Xn], dim=1)
            # TODO: do not refit F for now. shall we?
            #C = torch.zeros(n, rank).float()
            #F = torch.normal(C, 0.1)
            #self.F = F.float()

        # fix data loader with the new Ymat
        self.D.reset_Ymat(self.Ymat)

    def train_incremental(self,
                          Ymat_incr,
                          init_epochs=100,
                          alt_iters=10,
                          alt_f_iters=300,
                          alt_x_iters=300,
                          y_iters=200,
                          tenacity=7,
                          mod=5
                          ):

        self.prepare_increfit(Ymat_incr)

        print("Initializing Factors.....")
        self.num_epochs = init_epochs
        self.train_factors()

        if alt_iters % 2 == 1:
            alt_iters += 1

        print("Starting Alternate Training.....")

        for i in range(1, alt_iters):
            if i % 2 == 0:
                print(
                    "--------------------------------------------Training Factors. Iter#: "
                    + str(i)
                    + "-------------------------------------------------------"
                )
                self.num_epochs = alt_f_iters
                self.train_factors(
                    seed=False, early_stop=True, tenacity=tenacity, mod=mod
                )
            else:
                print(
                    "--------------------------------------------Training Local Model. Iter#: "
                    + str(i)
                    + "-------------------------------------------------------"
                )
                self.num_epochs = alt_x_iters
                T = np.array(self.X.cpu().detach())
                self.train_Xseq(
                    Ymat=T,
                    seq_model=self.Xseq,
                    start_index=self.start_index - self.val_len,
                    end_index=self.end_index - self.val_len,
                    num_epochs=self.num_epochs,
                    early_stop=True,
                    tenacity=tenacity,
                )

        print("--------- Training Local Global Hybrid Model------")
        self.num_epochs = y_iters
        Yseq_model = self.Yseq.seq
        self.train_Yseq(
            seq_model=Yseq_model,
            start_index=self.start_index - self.val_len,
            end_index=self.end_index - self.val_len,
            num_epochs=y_iters,
            early_stop=True,
            tenacity=tenacity)

    def train_Xseq(self,
                   Ymat,
                   seq_model=None,
                   start_index=0,
                   end_index=200,
                   num_epochs=20,
                   early_stop=False,
                   tenacity=3):
        seq = seq_model
        num_channels = self.num_channels_X
        kernel_size = self.kernel_size
        vbsize = min(self.vbsize, Ymat.shape[0] / 2)

        for p in seq.parameters():
            p.requires_grad = True

        TC = LocalModel(
            Ymat=Ymat,
            seq_model=seq,
            num_inputs=1,
            num_channels=num_channels,
            kernel_size=kernel_size,
            vbsize=vbsize,
            hbsize=self.hbsize,
            normalize=False,
            start_index=start_index,
            end_index=end_index,
            val_len=self.val_len,
            lr=self.lr,
            num_epochs=num_epochs,
        )

        TC.train_model(early_stop=early_stop, tenacity=tenacity)

        self.Xseq = TC.seq

    def train_factors(
        self,
        reg_X=0.0,
        reg_F=0.0,
        mod=5,
        early_stop=False,
        tenacity=3,
        ind=None,
        seed=False,
    ):
        self.D.reset()
        use_cuda = False
        print("test", use_cuda)
        if use_cuda:
            self.Xseq = self.Xseq.cuda()
        for p in self.Xseq.parameters():
            p.requires_grad = True

        l_F = [0.0]
        l_X = [0.0]
        l_X_temporal = [0.0]
        iter_count = 0
        vae = float("inf")
        scount = 0
        Xbest = self.X.clone()
        Fbest = self.F.clone()
        while self.D.epoch < self.num_epochs:
            last_epoch = self.D.epoch
            last_vindex = self.D.vindex
            last_hindex = self.D.hindex
            inp, out, vindex, hindex = self.D.next_batch(option=1)
            use_cuda = False
            if use_cuda:
                inp = inp.float().cuda()
                out = out.float().cuda()
            if iter_count % mod >= 0:
                l1 = self.step_factF_loss(inp, out, last_vindex, last_hindex, reg=reg_F)
                l_F = l_F + [l1.cpu().item()]
            if iter_count % mod >= 0:
                l1 = self.step_factX_loss(inp, out, last_vindex, last_hindex, reg=reg_X)
                l_X = l_X + [l1.cpu().item()]
            if seed == False and iter_count % mod == 1:
                l2 = self.step_temporal_loss_X(inp, last_vindex, last_hindex)
                l_X_temporal = l_X_temporal + [l2.cpu().item()]
            iter_count = iter_count + 1

            if self.D.epoch > last_epoch:
                print("Entering Epoch# ", self.D.epoch)
                print("Factorization Loss F: ", np.mean(l_F))
                print("Factorization Loss X: ", np.mean(l_X))
                print("Temporal Loss X: ", np.mean(l_X_temporal))
                if ind is None:
                    ind = np.arange(self.Ymat.shape[0])
                else:
                    ind = ind
                inp = self.predict_global(
                    ind,
                    last_step=self.end_index - self.val_len,
                    future=self.val_len,
                    cpu=False,
                )
                R = self.Ymat[ind, self.end_index - self.val_len : self.end_index]
                S = inp[:, -self.val_len : :]
                ve = np.abs(R - S).mean() / np.abs(R).mean()
                print("Validation Loss (Global): ", ve)
                if ve <= vae:
                    vae = ve
                    scount = 0
                    Xbest = self.X.clone()
                    Fbest = self.F.clone()
                    # Xseqbest = TemporalConvNet(
                    #     num_inputs=1,
                    #     num_channels=self.num_channels_X,
                    #     kernel_size=self.kernel_size,
                    #     dropout=self.dropout,
                    # )
                    # Xseqbest.load_state_dict(self.Xseq.state_dict())
                    Xseqbest = pickle.loads(pickle.dumps(self.Xseq))
                else:
                    scount += 1
                    if scount > tenacity and early_stop:
                        print("Early Stopped")
                        self.X = Xbest
                        self.F = Fbest
                        self.Xseq = Xseqbest
                        use_cuda = False
                        if use_cuda:
                            self.Xseq = self.Xseq.cuda()
                        break

    def create_Ycov(self):
        t0 = self.end_index + 1
        self.D.reset()
        # TODO calculate Ycov in the full matrix, maybe only added?
        self.D.hindex = 0
        Ycov = copy.deepcopy(self.Ymat[:, 0:t0])
        Ymat_now = self.Ymat[:, 0:t0]
        use_cuda = False
        if use_cuda:
            self.Xseq = self.Xseq.cuda()

        self.Xseq = self.Xseq.eval()

        while self.D.epoch < 1:
            last_epoch = self.D.epoch
            last_vindex = self.D.vindex
            last_hindex = self.D.hindex
            inp, out, vindex, hindex = self.D.next_batch(option=1)

            use_cuda = False
            if use_cuda:
                inp = inp.cuda()

            # Xin = self.tensor2d_to_temporal(self.X[:, last_hindex : last_hindex + inp.size(2)]).cuda()
            Xin = self.tensor2d_to_temporal(self.X[:, last_hindex: last_hindex + inp.size(2)]).cpu()
            Xout = self.temporal_to_tensor2d(self.Xseq(Xin)).cpu()
            Fout = self.F[self.D.I[last_vindex : last_vindex + out.size(0)], :]
            output = np.array(torch.matmul(Fout, Xout).detach())
            Ycov[
                last_vindex : last_vindex + output.shape[0],
                last_hindex + 1 : last_hindex + 1 + output.shape[1],
            ] = output

        for p in self.Xseq.parameters():
            p.requires_grad = True

        if self.period is None:
            Ycov_wc = np.zeros(shape=[Ycov.shape[0], 1, Ycov.shape[1]])
            if self.forward_cov:
                Ycov_wc[:, 0, 0:-1] = Ycov[:, 1::]
            else:
                Ycov_wc[:, 0, :] = Ycov
        else:
            Ycov_wc = np.zeros(shape=[Ycov.shape[0], 2, Ycov.shape[1]])
            if self.forward_cov:
                Ycov_wc[:, 0, 0:-1] = Ycov[:, 1::]
            else:
                Ycov_wc[:, 0, :] = Ycov
            Ycov_wc[:, 1, self.period - 1 : :] = Ymat_now[:, 0 : -(self.period - 1)]
        return Ycov_wc

    def train_Yseq(self,
                   seq_model=None,
                   start_index=0,
                   end_index=200,
                   num_epochs=20,
                   early_stop=False,
                   tenacity=7):
        Ycov = self.create_Ycov()
        self.Yseq = LocalModel(
            self.Ymat,
            seq_model=seq_model,
            num_inputs=1,
            num_channels=self.num_channels_Y,
            kernel_size=self.kernel_size_Y,
            dropout=self.dropout,
            vbsize=self.vbsize,
            hbsize=self.hbsize,
            num_epochs=num_epochs,
            lr=self.lr,
            val_len=self.val_len,
            test=True,
            start_index=start_index,
            end_index=end_index,
            normalize=False,
            start_date=self.start_date,
            freq=self.freq,
            covariates=self.covariates,
            use_time=self.use_time,
            dti=self.dti,
            Ycov=Ycov,
        )

        self.Yseq.train_model(early_stop=early_stop, tenacity=tenacity)

    def train_all_models(self,
                         init_epochs=100,
                         alt_iters=10,
                         alt_f_iters=300,
                         alt_x_iters=300,
                         y_iters=200,
                         tenacity=7,
                         mod=5
                         ):
        print("Initializing Factors.....")
        self.num_epochs = init_epochs
        self.train_factors()

        if alt_iters % 2 == 1:
            alt_iters += 1

        print("Starting Alternate Training.....")

        for i in range(1, alt_iters):
            if i % 2 == 0:
                print(
                    "--------------------------------------------Training Factors. Iter#: "
                    + str(i)
                    + "-------------------------------------------------------"
                )
                self.num_epochs = alt_f_iters
                self.train_factors(
                    seed=False, early_stop=True, tenacity=tenacity, mod=mod
                )
            else:
                print(
                    "--------------------------------------------Training Local Model. Iter#: "
                    + str(i)
                    + "-------------------------------------------------------"
                )
                self.num_epochs = alt_x_iters
                T = np.array(self.X.cpu().detach())
                self.train_Xseq(
                    Ymat=T,
                    seq_model=self.Xseq,
                    start_index=0,
                    end_index=self.end_index - self.val_len,
                    num_epochs=self.num_epochs,
                    early_stop=True,
                    tenacity=tenacity,
                )

        print("--------- Training Local Global Hybrid Model------")
        self.num_epochs = y_iters
        self.train_Yseq(
            start_index=0,
            end_index=self.end_index - self.val_len,
            num_epochs=y_iters,
            early_stop=True,
            tenacity=tenacity)

    def predict(
        self, ind=None, last_step=100, future=10, cpu=False, normalize=False, bsize=90
    ):

        if ind is None:
            ind = np.arange(self.Ymat.shape[0])
        if cpu:
            self.Xseq = self.Xseq.cpu()

        self.Yseq.seq = self.Yseq.seq.eval()
        self.Xseq = self.Xseq.eval()

        rg = max(
            1 + 2 * (self.kernel_size - 1) * 2 ** (len(self.num_channels_X) - 1),
            1 + 2 * (self.kernel_size_Y - 1) * 2 ** (len(self.num_channels_Y) - 1),
        )
        covs = self.Yseq.covariates[:, last_step - rg : last_step + future]
        # print(covs.shape)
        yc = self.predict_global(
            ind=ind,
            last_step=last_step,
            future=future,
            cpu=cpu,
            normalize=False,
            bsize=bsize,
        )
        if self.period is None:
            ycovs = np.zeros(shape=[yc.shape[0], 1, yc.shape[1]])
            if self.forward_cov:
                ycovs[:, 0, 0:-1] = yc[:, 1::]
            else:
                ycovs[:, 0, :] = yc
        else:
            ycovs = np.zeros(shape=[yc.shape[0], 2, yc.shape[1]])
            if self.forward_cov:
                ycovs[:, 0, 0:-1] = yc[:, 1::]
            else:
                ycovs[:, 0, :] = yc
            period = self.period
            while last_step + future - (period - 1) > last_step + 1:
                period += self.period
            ycovs[:, 1, period - 1 : :] = self.Ymat[
                :, last_step - rg : last_step + future - (period - 1)
            ]  ### this seems like we are looking ahead, but it will not use the last coordinate, which is the only new point added
        # print(ycovs.shape)

        Y = self.Yseq.predict_future(
            data_in=self.Ymat[ind, last_step - rg : last_step],
            covariates=covs,
            ycovs=ycovs,
            future=future,
            cpu=cpu,
            bsize=bsize,
            normalize=False,
        )

        if normalize:
            Y = Y - self.mini
            Y = Y * self.s[ind, None] + self.m[ind, None]
            return Y
        else:
            return Y

    def rolling_validation(self, Ymat, tau=24, n=7, bsize=90, cpu=False, alpha=0.3):
        prevX = self.X.clone()
        prev_index = self.end_index
        out = self.predict(
            last_step=self.end_index,
            future=tau,
            bsize=bsize,
            cpu=cpu,
            normalize=self.normalize,
        )
        out_global = self.predict_global(
            np.arange(self.Ymat.shape[0]),
            last_step=self.end_index,
            future=tau,
            cpu=cpu,
            normalize=self.normalize,
            bsize=bsize,
        )
        predicted_values = []
        actual_values = []
        predicted_values_global = []
        S = out[:, -tau::]
        S_g = out_global[:, -tau::]
        predicted_values += [S]
        predicted_values_global += [S_g]
        R = Ymat[:, self.end_index : self.end_index + tau]
        actual_values += [R]
        print("Current window wape: " + str(wape(S, R)))

        self.Xseq = self.Xseq.eval()
        self.Yseq.seq = self.Yseq.seq.eval()

        for i in range(n - 1):
            Xn = self.recover_future_X(
                last_step=self.end_index + 1,
                future=tau,
                num_epochs=100000,
                alpha=alpha,
                vanilla=True,
                cpu=True,
            )
            self.X = torch.cat([self.X, Xn], dim=1)
            self.end_index += tau
            out = self.predict(
                last_step=self.end_index,
                future=tau,
                bsize=bsize,
                cpu=cpu,
                normalize=self.normalize,
            )
            out_global = self.predict_global(
                np.arange(self.Ymat.shape[0]),
                last_step=self.end_index,
                future=tau,
                cpu=cpu,
                normalize=self.normalize,
                bsize=bsize,
            )
            S = out[:, -tau::]
            S_g = out_global[:, -tau::]
            predicted_values += [S]
            predicted_values_global += [S_g]
            R = Ymat[:, self.end_index : self.end_index + tau]
            actual_values += [R]
            print("Current window wape: " + str(wape(S, R)))

        predicted = np.hstack(predicted_values)
        predicted_global = np.hstack(predicted_values_global)
        actual = np.hstack(actual_values)

        dic = {}
        dic["wape"] = wape(predicted, actual)
        dic["mape"] = mape(predicted, actual)
        dic["smape"] = smape(predicted, actual)
        dic["mae"] = np.abs(predicted - actual).mean()
        dic["rmse"] = np.sqrt(((predicted - actual) ** 2).mean())
        dic["nrmse"] = dic["rmse"] / np.sqrt(((actual) ** 2).mean())

        dic["wape_global"] = wape(predicted_global, actual)
        dic["mape_global"] = mape(predicted_global, actual)
        dic["smape_global"] = smape(predicted_global, actual)
        dic["mae_global"] = np.abs(predicted_global - actual).mean()
        dic["rmse_global"] = np.sqrt(((predicted_global - actual) ** 2).mean())
        dic["nrmse_global"] = dic["rmse"] / np.sqrt(((actual) ** 2).mean())

        baseline = Ymat[:, Ymat.shape[1] - n * tau - tau : Ymat.shape[1] - tau]
        dic["baseline_wape"] = wape(baseline, actual)
        dic["baseline_mape"] = mape(baseline, actual)
        dic["baseline_smape"] = smape(baseline, actual)
        self.X = prevX
        self.end_index = prev_index

        return dic
