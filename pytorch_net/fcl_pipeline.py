import os
import time
import numpy as np
from PIL import Image, ImageFilter
import cv2

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.utils as vutils
import torchvision.transforms.functional as transform
from tensorboardX import SummaryWriter

from dataset.BSD500 import *
from models.HED import HED
from models.FCL import FCL
# from models.FCL_ablation import FCL
from models.BDCN import BDCN
from utils import AverageMeter


class FCLPipeline():
    def __init__(self, cfg):

        self.cfg = self.cfg_checker(cfg)
        self.root = '/'.join(['../ckpt', self.cfg.path.split('.')[0]])
        self.cur_lr = self.cfg.TRAIN.init_lr

        if self.cfg.TRAIN.disp_iter < self.cfg.TRAIN.update_iter:
            self.cfg.TRAIN.disp_iter = self.cfg.TRAIN.update_iter

        self.log_dir = os.path.join(self.root + '/log/', self.cfg.NAME + self.cfg.time)
        self.writer = SummaryWriter(self.log_dir)

        self.writer.add_text('cfg', str(self.cfg))

        # ######################## Dataset ################################################3
        dataset = BSD500Dataset(self.cfg)
        self.data_loader = torch.utils.data.DataLoader(
            dataset,
            batch_size=self.cfg.TRAIN.batchsize,
            shuffle=True,
            num_workers=self.cfg.TRAIN.num_workers)

        dataset_test = BSD500DatasetTest(self.cfg)
        self.data_test_loader = torch.utils.data.DataLoader(
            dataset_test,
            batch_size=1,
            shuffle=False,
            num_workers=self.cfg.TRAIN.num_workers)

        # ######################## Model ################################################3
        if self.cfg.MODEL.mode == 'HED':
            self.model = HED(self.cfg, self.writer)
        elif self.cfg.MODEL.mode == 'FCL' or self.cfg.MODEL.mode == 'RCF':
            self.model = FCL(self.cfg, self.writer)
        elif self.cfg.MODEL.mode == 'BDCN':
            self.model = BDCN(self.cfg, self.writer)

        self.model = self.model.cuda()
        # print(self.model)  # check the parameters of the model

        ### loss function
        if self.cfg.MODEL.loss_func_logits:
            self.loss_function = F.binary_cross_entropy_with_logits
        else:
            self.loss_function = F.binary_cross_entropy

        # ######################## Optimizer ################################################3
        init_lr = self.cfg.TRAIN.init_lr
        self.lr_cof = self.cfg.TRAIN.lr_cof

        if self.cfg.TRAIN.update_method == 'SGD':
            if self.cfg.MODEL.mode == 'RCF':
                params_lr_1 = list(self.model.conv1_1.parameters()) \
                              + list(self.model.conv1_2.parameters()) \
                              + list(self.model.conv2_1.parameters()) \
                              + list(self.model.conv2_2.parameters()) \
                              + list(self.model.conv3_1.parameters()) \
                              + list(self.model.conv3_2.parameters()) \
                              + list(self.model.conv3_3.parameters()) \
                              + list(self.model.conv4_1.parameters()) \
                              + list(self.model.conv4_2.parameters()) \
                              + list(self.model.conv4_3.parameters())
                params_lr_100 = list(self.model.conv5_1.parameters()) \
                                + list(self.model.conv5_2.parameters()) \
                                + list(self.model.conv5_3.parameters())
                params_lr_001 = list(self.model.dsn1_1.parameters()) \
                                + list(self.model.dsn1_2.parameters()) \
                                + list(self.model.dsn2_1.parameters()) \
                                + list(self.model.dsn2_2.parameters()) \
                                + list(self.model.dsn3_1.parameters()) \
                                + list(self.model.dsn3_2.parameters()) \
                                + list(self.model.dsn3_3.parameters()) \
                                + list(self.model.dsn4_1.parameters()) \
                                + list(self.model.dsn4_2.parameters()) \
                                + list(self.model.dsn4_3.parameters()) \
                                + list(self.model.dsn5_1.parameters()) \
                                + list(self.model.dsn5_2.parameters()) \
                                + list(self.model.dsn5_3.parameters()) \
                                + list(self.model.dsn1.parameters()) \
                                + list(self.model.dsn2.parameters()) \
                                + list(self.model.dsn3.parameters()) \
                                + list(self.model.dsn4.parameters()) \
                                + list(self.model.dsn5.parameters())
                params_lr_0001 = self.model.new_score_weighting.parameters()
            else:
                params_lr_1 = list(self.model.conv1.parameters()) \
                              + list(self.model.conv2.parameters()) \
                              + list(self.model.conv3.parameters()) \
                              + list(self.model.conv4.parameters())
                params_lr_100 = self.model.conv5.parameters()
                params_lr_001 = list(self.model.dsn1.parameters()) \
                                + list(self.model.dsn2.parameters()) \
                                + list(self.model.dsn3.parameters()) \
                                + list(self.model.dsn4.parameters()) \
                                + list(self.model.dsn5.parameters())
                params_lr_0001 = self.model.new_score_weighting.parameters()

            optim_paras_list = [{'params': params_lr_1},
                                {'params': params_lr_100, 'lr': init_lr * self.lr_cof[1]},
                                {'params': params_lr_001, 'lr': init_lr * self.lr_cof[2]},
                                {'params': params_lr_0001, 'lr': init_lr * self.lr_cof[3]}
                                ]

            self.optim = torch.optim.SGD(optim_paras_list, lr=init_lr, momentum=0.9, weight_decay=1e-4)

        elif self.cfg.TRAIN.update_method in ['Adam', 'Adam-sgd']:
            self.optim = torch.optim.Adam(self.model.parameters(), lr=init_lr)  # weight_decay=1e-4

        self.optim.zero_grad()

        # ######################## load pretrain parameters and reset Optimizer ################################################3
        if self.cfg.TRAIN.resume:
            # selectively load same parameters
            self.param_path = self.cfg.TRAIN.param_path
            pre = torch.load(self.param_path)
            print('Loading pretrained parameters from:{}'.format(self.param_path))
            model_dict = self.model.state_dict()
            print('-' * 30, 'same keys(default exclude newscoreweighting): ...')
            state_dict = {k: v for k, v in pre.items() if k in model_dict.keys() and 'new_score_weighting' not in k}

            model_dict.update(state_dict)
            self.model.load_state_dict(model_dict)

            # freeze optimizer parameters for not back forward
            for name, m in self.model.named_modules():
                if isinstance(m, nn.BatchNorm2d) and 'cls' not in name:
                    # print("---- in bn layer")
                    # print(name)
                    m.eval()
                    print("---- Freezing Weight/Bias of BatchNorm2D.")
                    m.weight.requires_grad = False
                    m.bias.requires_grad = False

            flag = 0  # whether freeze parameters
            for name, value in self.model.named_parameters():
                if name in state_dict.keys():
                    if self.cfg.TRAIN.freeze_pretrained_param:
                        value.requires_grad = False
                        print('--> require no grad:{}'.format(name))
                    else:
                        flag = 1
                        pass
            if flag:
                print('-' * 30, '\n  Not freeze all pretrained parameters!\n', '-' * 30)

            self.optim = torch.optim.Adam(filter(lambda p: p.requires_grad, self.model.parameters()),
                                          lr=init_lr)  # 0.0001
            self.optim.zero_grad()

            cnt = 0
            print('-' * 30, 'Parameters Requires Gradient', '-' * 30)
            for name, p in self.model.named_parameters():
                if p.requires_grad:
                    cnt = cnt + 1
                    print('{}: model parameters:{}'.format(cnt, name))
                else:
                    pass

            # according to pre knowledge, set initialization for specific layers
            if self.cfg.TRAIN.re_init_fuseweight:  # choose to re-init weight fusing
                weight_init = torch.tensor([0.5, 1, 0.5, 0.5, 0.5, 1]).reshape(1, 6, 1, 1).cuda()
                self.model.new_score_weighting.weight.data.copy_(weight_init)
                self.model.new_score_weighting.bias.data.fill_(0.0)
            else:
                pass

            print('-' * 20)
            print('self.model.new_score_weighting.weight.data requires grad:{}'.format(
                self.model.new_score_weighting.weight.requires_grad))
            print('self.model.new_score_weighting.weight.data weight:{}'.format(self.model.new_score_weighting.weight))

    def train(self):
        self.model.train()

        batch_time = AverageMeter()
        data_time = AverageMeter()

        self.final_loss = 0
        tic = time.time()
        for cur_epoch in range(self.cfg.TRAIN.nepoch):

            count = 1  # record model among an epoch(per 10000)

            for ind, (data, target) in enumerate(self.data_loader):
                cur_iter = cur_epoch * len(self.data_loader) + ind + 1

                data, target = data.cuda(), target.cuda()
                data_time.update(time.time() - tic)

                if self.cfg.TRAIN.fusion_train:
                    dsn1, dsn2, dsn3, dsn4, dsn5, dsn6, side_fusion = self.model(data)
                    # { added lstm
                elif self.cfg.MODEL.ClsHead:
                    dsn1, dsn2, dsn3, dsn4, dsn5, dsn6, dsn7 = self.model(data)
                elif self.cfg.MODEL.LSTM or self.cfg.MODEL.LSTM_bu:
                    dsn1, dsn2, dsn3, dsn4, dsn5 = self.model(data)
                else:
                    dsn1, dsn2, dsn3, dsn4, dsn5, dsn6 = self.model(data)  # if cls_head, dsn6 in 0-1

                # loss_function_logits = True
                if not self.cfg.MODEL.loss_func_logits and not self.cfg.MODEL.sigmoid_attention and not self.cfg.MODEL.vgg_attention:
                    dsn1 = torch.sigmoid(dsn1)
                    dsn2 = torch.sigmoid(dsn2)
                    dsn3 = torch.sigmoid(dsn3)
                    dsn4 = torch.sigmoid(dsn4)
                    dsn5 = torch.sigmoid(dsn5)
                    dsn6 = torch.sigmoid(dsn6)
                    # if self.cfg.MODEL.ClsHead:
                    #     dsn7 = torch.sigmoid(dsn7)

                # ############################# Compute Loss ########################################
                if self.cfg.MODEL.loss_balance_weight:
                    if self.cfg.MODEL.focal_loss:
                        focal_weight1 = self.edge_weight(target, dsn1, gamma=2)
                        focal_weight2 = self.edge_weight(target, dsn2, gamma=2)
                        focal_weight3 = self.edge_weight(target, dsn3, gamma=2)
                        focal_weight4 = self.edge_weight(target, dsn4, gamma=2)
                        focal_weight5 = self.edge_weight(target, dsn5, gamma=2)
                        # focal_weight6 = self.edge_weight(target, dsn6, gamma=2)
                    else:
                        cur_weight = self.edge_weight(target, balance=self.cfg.TRAIN.gamma)
                        self.writer.add_histogram('weight: ', cur_weight.clone().cpu().data.numpy(), cur_epoch)
                else:
                    cur_weight = None

                # boundary weighted attention
                if self.cfg.MODEL.boundary_weighted_attention:
                    gt = target.clone().detach().cpu()
                    b, c, w, h = list(gt.size())
                    gt_img = transform.to_pil_image(gt.reshape(1, w, h), 'L')
                    gt_gauss = gt_img.filter(ImageFilter.GaussianBlur(radius=1))
                    gt_gauss = transform.to_tensor(gt_gauss)
                    boundary_weight = (torch.ones((1, w, h)) - gt_gauss).cuda()

                    dsn1 = dsn1.mul(boundary_weight)
                    dsn2 = dsn2.mul(boundary_weight)
                    dsn3 = dsn3.mul(boundary_weight)
                    dsn4 = dsn4.mul(boundary_weight)
                    dsn5 = dsn5.mul(boundary_weight)
                    # dsn6 = dsn6.mul(boundary_weight) #unrational, may use Canny as transcendant

                if self.cfg.MODEL.loss_func_logits == 'Dice':  # Dice Loss or reDice Loss
                    self.loss1 = self.loss_function(dsn1.float(), target.float())
                    self.loss2 = self.loss_function(dsn2.float(), target.float())
                    self.loss3 = self.loss_function(dsn3.float(), target.float())
                    self.loss4 = self.loss_function(dsn4.float(), target.float())
                    self.loss5 = self.loss_function(dsn5.float(), target.float())
                    self.loss6 = self.loss_function(dsn6.float(), target.float())
                else:  # loss_function_logits = False
                    cur_reduce = self.cfg.MODEL.loss_reduce
                    if self.cfg.MODEL.focal_loss:
                        self.loss1 = self.loss_function(dsn1.float(), target.float(), weight=focal_weight1,
                                                        reduce=cur_reduce)
                        self.loss2 = self.loss_function(dsn2.float(), target.float(), weight=focal_weight2,
                                                        reduce=cur_reduce)
                        self.loss3 = self.loss_function(dsn3.float(), target.float(), weight=focal_weight3,
                                                        reduce=cur_reduce)
                        self.loss4 = self.loss_function(dsn4.float(), target.float(), weight=focal_weight4,
                                                        reduce=cur_reduce)
                        self.loss5 = self.loss_function(dsn5.float(), target.float(), weight=focal_weight5,
                                                        reduce=cur_reduce)
                        if self.cfg.MODEL.ClsHead:
                            self.loss6 = self.loss_function(dsn6.float(), target.float(), weight=focal_weight6,
                                                            reduce=cur_reduce)
                            self.loss7 = self.loss_function(dsn7.float(), target.float(), weight=focal_weight7,
                                                            reduce=cur_reduce)
                    elif self.cfg.MODEL.LSTM or self.cfg.MODEL.LSTM_bu:
                        self.loss1 = self.loss_function(dsn1.float(), target.float(), weight=cur_weight,
                                                        reduce=cur_reduce)
                        self.loss2 = self.loss_function(dsn2.float(), target.float(), weight=cur_weight,
                                                        reduce=cur_reduce)
                        self.loss3 = self.loss_function(dsn3.float(), target.float(), weight=cur_weight,
                                                        reduce=cur_reduce)
                        self.loss4 = self.loss_function(dsn4.float(), target.float(), weight=cur_weight,
                                                        reduce=cur_reduce)
                        self.loss5 = self.loss_function(dsn5.float(), target.float(), weight=cur_weight,
                                                        reduce=cur_reduce)
                        if self.cfg.MODEL.ClsHead:
                            self.loss6 = self.loss_function(dsn6.float(), target.float(), weight=cur_weight,
                                                            reduce=cur_reduce)
                            self.loss7 = self.loss_function(dsn7.float(), target.float(), weight=cur_weight,
                                                            reduce=cur_reduce)
                    else:
                        self.loss1 = self.loss_function(dsn1.float(), target.float(), weight=cur_weight,
                                                        reduce=cur_reduce)
                        self.loss2 = self.loss_function(dsn2.float(), target.float(), weight=cur_weight,
                                                        reduce=cur_reduce)
                        self.loss3 = self.loss_function(dsn3.float(), target.float(), weight=cur_weight,
                                                        reduce=cur_reduce)
                        self.loss4 = self.loss_function(dsn4.float(), target.float(), weight=cur_weight,
                                                        reduce=cur_reduce)
                        self.loss5 = self.loss_function(dsn5.float(), target.float(), weight=cur_weight,
                                                        reduce=cur_reduce)
                        if self.cfg.MODEL.ClsHead:
                            self.loss6 = self.loss_function(dsn6.float(), target.float(), weight=cur_weight,
                                                            reduce=cur_reduce)
                            self.loss7 = self.loss_function(dsn7.float(), target.float(), weight=cur_weight,
                                                            reduce=cur_reduce)
                        else:
                            self.loss6 = self.loss_function(dsn6.float(), target.float(), weight=cur_weight,
                                                            reduce=cur_reduce)

                loss_weight_list = self.cfg.MODEL.loss_weight_list
                # assert( len(loss_weight_list)==6, "len(loss_weight) should be 6" )

                if self.cfg.MODEL.ClsHead and self.cfg.MODEL.LSTM_bu:
                    loss = [self.loss1, self.loss2, self.loss3, self.loss4, self.loss5, self.loss6, self.loss7]
                elif self.cfg.MODEL.ClsHead:
                    loss = [self.loss1, self.loss2, self.loss3, self.loss4, self.loss5, self.loss6, self.loss7]
                elif self.cfg.MODEL.LSTM or self.cfg.MODEL.LSTM_bu:
                    loss = [self.loss1, self.loss2, self.loss3, self.loss4, self.loss5]
                else:
                    loss = [self.loss1, self.loss2, self.loss3, self.loss4, self.loss5, self.loss6]

                self.loss = sum([x * y for x, y in zip(loss_weight_list, loss)])

                if self.cfg.TRAIN.fusion_train:
                    self.loss_fusion = self.loss_function(side_fusion.float(), target.float(), weight=cur_weight, reduce=cur_reduce)
                    self.loss = self.loss + self.loss_fusion

                self.loss = self.loss / self.cfg.TRAIN.update_iter
                self.final_loss += self.loss

                if self.cfg.MODEL.loss_func_logits and cur_reduce:
                    if np.isnan(float(self.loss.item())):
                        raise ValueError('loss is nan while training')

                self.loss.backward()

                # ############################# Update Gradients ########################################

                if (cur_iter % self.cfg.TRAIN.update_iter) == 0:
                    self.optim.step()
                    self.optim.zero_grad()

                    self.final_loss_show = self.final_loss
                    self.final_loss = 0

                batch_time.update(time.time() - tic)
                tic = time.time()

                if ((ind + 1) % self.cfg.TRAIN.disp_iter) == 0:
                    if self.cfg.MODEL.ClsHead:
                        print_str = 'Epoch: [{}][{}/{}], Time: {:.2f}, Data: {:.2f}, lr: {:.11f}, \n \
                                      final_loss: {:.6f}, loss1:{:.6f}, loss2:{:.6f}, loss3:{:.6f}, loss4:{:.6f}, loss5:{:.6f}, loss6:{:.6f}, loss7:{:.6f}\n '.format(
                            cur_epoch, ind, len(self.data_loader), batch_time.average(), data_time.average(),
                            self.cur_lr, self.final_loss_show, self.loss1, self.loss2, self.loss3, self.loss4,
                            self.loss5, self.loss6, self.loss7)
                    elif self.cfg.MODEL.LSTM or self.cfg.MODEL.LSTM_bu:  # 20200608 add loss6 # delete dsn6 20200610
                        print_str = 'Epoch: [{}][{}/{}], Time: {:.2f}, Data: {:.2f}, lr: {:.11f}, \n \
                                      final_loss: {:.6f}, loss1:{:.6f}, loss2:{:.6f}, loss3:{:.6f}, \
                                      loss4:{:.6f}, loss5:{:.6f}\n '.format(cur_epoch, ind, \
                                                                            len(self.data_loader), batch_time.average(),
                                                                            data_time.average(), \
                                                                            self.cur_lr, self.final_loss_show,
                                                                            self.loss1, self.loss2, \
                                                                            self.loss3, self.loss4, self.loss5)
                    else:
                        print_str = 'Epoch: [{}][{}/{}], Time: {:.2f}, Data: {:.2f}, lr: {:.11f}, \n \
                                      final_loss: {:.6f}, loss1:{:.6f}, loss2:{:.6f}, loss3:{:.6f}, \loss4:{:.6f}, loss5:{:.6f}, loss6:{:.6f}\n '.format(
                            cur_epoch, ind, len(self.data_loader), batch_time.average(), data_time.average(),
                            self.cur_lr, self.final_loss_show, self.loss1, self.loss2, self.loss3, self.loss4,
                            self.loss5, self.loss6)

                    print(print_str)

                    # show loss
                    self.writer.add_scalar('loss/loss1', self.loss1.item(), cur_iter)
                    self.writer.add_scalar('loss/loss2', self.loss2.item(), cur_iter)
                    self.writer.add_scalar('loss/loss3', self.loss3.item(), cur_iter)
                    self.writer.add_scalar('loss/loss4', self.loss4.item(), cur_iter)
                    self.writer.add_scalar('loss/loss5', self.loss5.item(), cur_iter)

                    if not self.cfg.MODEL.LSTM and not self.cfg.MODEL.LSTM_bu:
                        self.writer.add_scalar('loss/loss6', self.loss6.item(), cur_iter)

                    if self.cfg.MODEL.ClsHead and self.cfg.MODEL.LSTM_bu:
                        self.writer.add_scalar('loss/loss6', self.loss6.item(), cur_iter)
                        self.writer.add_scalar('loss/loss7', self.loss7.item(), cur_iter)
                    elif self.cfg.MODEL.ClsHead:
                        self.writer.add_scalar('loss/loss6', self.loss6.item(), cur_iter)
                        self.writer.add_scalar('loss/loss7', self.loss7.item(), cur_iter)

                    if self.cfg.TRAIN.fusion_train:
                        self.writer.add_scalar('loss/loss_fusion', self.loss_fusion.item(), cur_iter)
                        print('loss_fusion:{}'.format(self.loss_fusion))

                    self.writer.add_scalar('final_loss', self.final_loss_show.item(), cur_iter)

                    # add lstm without fusion
                    if not self.cfg.MODEL.LSTM and self.cfg.MODEL.LSTM_bu:
                        if self.cfg.MODEL.ClsHead and len(loss) == 7:
                            self.tensorboard_summary(cur_iter)

                # record parameters per 10000, which is finer
                if ((ind + 1) % 10000) == 0:
                    suffix_latest = 'epoch_{}_{}.pth'.format(cur_epoch, count)
                    print('=======> saving model: {}'.format(suffix_latest))
                    model_save_path = os.path.join(self.log_dir, suffix_latest)
                    torch.save(self.model.state_dict(), model_save_path)
                    count = count + 1
                # end }

            # lr update
            if self.cfg.TRAIN.update_method == 'SGD':
                self.cur_lr = self.step_learning_rate(self.optim, self.cur_lr, self.cfg.TRAIN.lr_list, (cur_epoch + 1))
            elif self.cfg.TRAIN.update_method == 'Adam':
                self.cur_lr = self.StepLR(self.optim, self.cur_lr, self.cfg.TRAIN.lr_list, (cur_epoch + 1))

            # Test
            if ((cur_epoch + 1) % self.cfg.TRAIN.test_iter) == 0:
                self.test(cur_epoch)

            self.writer.add_text('epoch', 'cur_epoch is ' + str(cur_epoch), cur_epoch)
            self.writer.add_text('loss', str(print_str))

            ### save model
            if ((cur_epoch + 1) % self.cfg.TRAIN.save_iter) == 0:
                print('=======> saving model')
                suffix_latest = 'epoch_{}.pth'.format(cur_epoch)
                model_save_path = os.path.join(self.log_dir, suffix_latest)
                torch.save(self.model.state_dict(), model_save_path)

        self.writer.close()

    def tensorboard_summary(self, cur_epoch):
        # weight
        print('weight: ')
        print(self.model.new_score_weighting.weight)
        print(self.model.new_score_weighting.bias)
        self.writer.add_histogram('new_score_weighting/weight: ',
                                  self.model.new_score_weighting.weight.clone().cpu().data.numpy(), cur_epoch)
        self.writer.add_histogram('new_score_weighting/bias: ',
                                  self.model.new_score_weighting.bias.clone().cpu().data.numpy(), cur_epoch)

        if self.cfg.TRAIN.fusion_train:
            self.writer.add_histogram('fusion_weighting/weight: ',
                                      self.model.side_fusion_weighting.weight.clone().cpu().data.numpy(), cur_epoch)
            self.writer.add_histogram('fusion_weighting/bias: ',
                                      self.model.side_fusion_weighting.bias.clone().cpu().data.numpy(), cur_epoch)
            print(list(self.model.side_fusion_weighting.weight), self.model.side_fusion_weighting.bias)

        if self.cfg.MODEL.backbone == 'resnet50' or self.cfg.MODEL.mode == 'RCF' or self.cfg.MODEL.mode == 'BDCN' or self.cfg.MODEL.mode == 'RCF_bilateral_attention':
            return

        # conv5 params
        conv5_index = -3 if self.cfg.MODEL.backbone == 'vgg16_bn' else -2
        self.writer.add_histogram('conv5/a_weight: ', self.model.conv5[conv5_index].weight.clone().cpu().data.numpy(),
                                  cur_epoch)
        self.writer.add_histogram('conv5/a_bias: ', self.model.conv5[conv5_index].bias.clone().cpu().data.numpy(),
                                  cur_epoch)
        self.writer.add_histogram('conv5/b_weight_grad: ',
                                  self.model.conv5[conv5_index].weight.grad.clone().cpu().data.numpy(), cur_epoch)

        self.writer.add_histogram('conv5/b_bias_grad: ',
                                  self.model.conv5[conv5_index].bias.grad.clone().cpu().data.numpy(), cur_epoch)
        self.writer.add_histogram('conv5/c_output: ', self.model.conv5_output.clone().cpu().data.numpy(), cur_epoch)

    def edge_weight(self, target, pred=None, balance=1.1, gamma=2):
        h, w = target.shape[2:]
        if self.cfg.MODEL.focal_loss and self.cfg.DATA.gt_mode == 'gt_part':
            n, c, h, w = target.size()
            balance_weights = np.zeros((n, c, h, w))
            focal_weights = np.zeros((n, c, h, w))
            for i in range(n):
                t = target[i, :, :, :].cpu().data.numpy()
                pos = (t == 1).sum()
                neg = (t == 0).sum()
                valid = neg + pos
                balance_weights[i, t == 1] = neg * 1. / valid
                balance_weights[i, t == 0] = pos * balance / valid  # pos  / valid

                f = pred[i, :, :, :].detach().cpu().data.numpy()
                focal_weights[i, t == 1] = 1 - f[t == 1]
                focal_weights[i, t == 0] = f[t == 0]
                focal_weights = focal_weights ** gamma
            weights = torch.Tensor(balance_weights * focal_weights)
            weights = weights.cuda()
            return weights
        elif self.cfg.DATA.gt_mode == 'gt_part':
            n, c, h, w = target.size()
            weights = np.zeros((n, c, h, w))
            for i in range(n):
                t = target[i, :, :, :].cpu().data.numpy()
                pos = (t == 1).sum()
                neg = (t == 0).sum()
                valid = neg + pos
                weights[i, t == 1] = neg * 1. / valid
                weights[i, t == 0] = pos * balance / valid
            weights = torch.Tensor(weights)
            weights = weights.cuda()
            return weights
        else:
            weight_p = torch.sum(target) / (h * w)
            weight_n = 1 - weight_p

            res = target.clone()
            res[target == 0] = weight_p
            res[target > 0] = weight_n
            assert ((weight_p + weight_n) == 1, "weight_p + weight_n !=1")

            return res

    def edge_pos_weight(self, target):
        h, w = target.shape[2:]
        weight_p = torch.sum(target) / (h * w)
        weight_n = 1 - weight_p

        pos_weight = weight_n / weight_p

        res = target.clone()
        res = (1 - weight_n)

        return res, pos_weight

    def poly_learning_rate(self, optimizer, base_lr, curr_iter, max_iter, power=0.9):

        """poly learning rate policy"""
        lr = base_lr * (1 - float(curr_iter) / max_iter) ** power

        assert (len(optimizer.param_groups) == 4, 'num of len(optimizer.param_groups)')
        for index, param_group in enumerate(optimizer.param_groups):
            param_group['lr'] = lr * self.lr_cof[index]

        return lr

    def step_learning_rate(self, optimizer, lr, lr_list, cur_epoch):

        if cur_epoch not in lr_list:
            return lr

        lr = lr / 10
        # assert( len(optimizer.param_groups)==5 'num of len(optimizer.param_groups)' )
        for index, param_group in enumerate(optimizer.param_groups):
            param_group['lr'] = lr * self.lr_cof[index]
            # param_group['lr'] = lr

        self.writer.add_text('LR', 'lr = ' + str(lr) + ' at step: ' + str(cur_epoch))

        return lr

    # add to do reduce lr in Adam 2020-08-27
    def StepLR(self, optimizer, lr, lr_list, cur_epoch):

        if cur_epoch not in lr_list:
            return lr

        print('lr change: {} -> {}'.format(lr, lr / 10))
        lr = lr / 10;
        for index, param_group in enumerate(optimizer.param_groups):
            param_group['lr'] = lr

        self.writer.add_text('LR', 'lr = ' + str(lr) + ' at step: ' + str(cur_epoch))

        return lr

    def test(self, cur_epoch=None, param_path=None):
        self.model.eval()
        print(' ---------Test, cur_epoch: ', cur_epoch)
        # Forward
        for ind, item in enumerate(self.data_test_loader):
            (data, img_filename) = item
            # (data, target) = item
            data = data.cuda()

            if self.cfg.TRAIN.fusion_train:
                dsn1, dsn2, dsn3, dsn4, dsn5, dsn6, side_fusion = self.model(data)
            elif self.cfg.MODEL.ClsHead and self.cfg.MODEL.LSTM_bu:
                dsn1, dsn2, dsn3, dsn4, dsn5, dsn6, dsn7 = self.model(data)
            elif self.cfg.MODEL.LSTM or self.cfg.MODEL.LSTM_bu:
                dsn1, dsn2, dsn3, dsn4, dsn5 = self.model(data)
            elif self.cfg.MODEL.ClsHead:
                dsn1, dsn2, dsn3, dsn4, dsn5, dsn6, dsn7 = self.model(data)
            else:
                dsn1, dsn2, dsn3, dsn4, dsn5, dsn6 = self.model(data)

            # pdb.set_trace()
            input_show = vutils.make_grid(data, normalize=True, scale_each=True)
            if self.cfg.MODEL.loss_func_logits:
                dsn1 = torch.sigmoid(dsn1)
                dsn2 = torch.sigmoid(dsn2)
                dsn3 = torch.sigmoid(dsn3)
                dsn4 = torch.sigmoid(dsn4)
                dsn5 = torch.sigmoid(dsn5)
                if self.cfg.MODEL.ClsHead:
                    dsn6 = torch.sigmoid(dsn6)  # dsn6
                    dsn7 = torch.sigmoid(dsn7)
                elif not self.cfg.MODEL.LSTM and not self.cfg.MODEL.LSTM_bu:
                    dsn6 = torch.sigmoid(dsn6)

            if self.cfg.TRAIN.fusion_train:
                dsn7 = side_fusion
            elif self.cfg.MODEL.ClsHead:
                dsn7 = dsn7
            else:
                dsn7 = (dsn1 + dsn2 + dsn3 + dsn4 + dsn5) / 5.0

            if self.cfg.MODEL.ClsHead and self.cfg.MODEL.LSTM_bu:
                results = [dsn1, dsn2, dsn3, dsn4, dsn5, dsn6, dsn7]  # change dsn6->dsn7
            elif self.cfg.MODEL.LSTM or self.cfg.MODEL.LSTM_bu:
                results = [dsn1, dsn2, dsn3, dsn4, dsn5, dsn7]
            else:
                results = [dsn1, dsn2, dsn3, dsn4, dsn5, dsn6, dsn7]

            # save results
            self.save_mat(results, img_filename, cur_epoch)

            dsn1_show = vutils.make_grid(dsn1.data, normalize=True, scale_each=True)
            dsn2_show = vutils.make_grid(dsn2.data, normalize=True, scale_each=True)
            dsn3_show = vutils.make_grid(dsn3.data, normalize=True, scale_each=True)
            dsn4_show = vutils.make_grid(dsn4.data, normalize=True, scale_each=True)
            dsn5_show = vutils.make_grid(dsn5.data, normalize=True, scale_each=True)

            if self.cfg.MODEL.ClsHead and self.cfg.MODEL.LSTM_bu:
                dsn6_show = vutils.make_grid(dsn6.data, normalize=False, scale_each=True)
                dsn7_show = vutils.make_grid(dsn7.data, normalize=False, scale_each=True)
            elif self.cfg.MODEL.LSTM or self.cfg.MODEL.LSTM_bu:
                # dsn6_show = vutils.make_grid(dsn6.data, normalize=False, scale_each=True)
                dsn7_show = vutils.make_grid(dsn7.data, normalize=False,
                                             scale_each=True)
            else:
                dsn6_show = vutils.make_grid(dsn6.data, normalize=False, scale_each=True)
                dsn7_show = vutils.make_grid(dsn7.data, normalize=False, scale_each=True)
            # target_show = vutils.make_grid(target.data, normalize=True, scale_each=True)

            # record the results in tensorboard
            if (ind + 1) % self.cfg.SAVE.board_freq == 0:
                self.writer.add_image(img_filename[0] + '/aa_input', input_show, cur_epoch)
                # add lstm without fusion
                if not self.cfg.MODEL.LSTM and not self.cfg.MODEL.LSTM_bu:
                    self.writer.add_image(img_filename[0] + '/ab_dsn6', dsn6_show, cur_epoch)
                elif self.cfg.MODEL.ClsHead and self.cfg.MODEL.LSTM_bu:
                    self.writer.add_image(img_filename[0] + '/ab_dsn6', dsn6_show, cur_epoch)

                self.writer.add_image(img_filename[0] + '/ab_dsn7', dsn7_show,
                                      cur_epoch)
                # self.writer.add_image(img_filename[0]+'/ac_target', target_show, cur_epoch)
                self.writer.add_image(img_filename[0] + '/dsn1', dsn1_show, cur_epoch)
                self.writer.add_image(img_filename[0] + '/dsn2', dsn2_show, cur_epoch)
                self.writer.add_image(img_filename[0] + '/dsn3', dsn3_show, cur_epoch)
                self.writer.add_image(img_filename[0] + '/dsn4', dsn4_show, cur_epoch)
                self.writer.add_image(img_filename[0] + '/dsn5', dsn5_show, cur_epoch)

        # set model mode to train()
        self.model.train()

    def save_mat(self, results, img_filename, cur_epoch, normalize=True, test=False):
        path = os.path.join(self.log_dir, 'results_mat')
        if cur_epoch == 0 or not os.path.exists(path):
            self.makedir(os.path.join(self.log_dir, 'results_mat'))

        self.makedir(os.path.join(self.log_dir, 'results_mat', str(cur_epoch)))

        if test:
            num = 9
        else:
            num = 8

        for dsn_ind in range(1, num):
            if self.cfg.MODEL.LSTM and dsn_ind == 7:
                break
            self.makedir(os.path.join(self.log_dir, 'results_mat', str(cur_epoch), 'dsn' + str(dsn_ind))))

        for ind, each_dsn in enumerate(results):
            each_dsn = each_dsn.data.cpu().numpy()
            each_dsn = np.squeeze(each_dsn)

            save_path = os.path.join(self.log_dir, 'results_mat', str(cur_epoch), 'dsn' + str(ind + 1), img_filename[0] + '.png')

            if self.cfg.SAVE.MAT.normalize and normalize:  # false when test ms
                # print(np.max(each_dsn))
                each_dsn = each_dsn / np.max(each_dsn)

            # scipy.io.savemat(save_path, dict({'edge': each_dsn}))
            cv2.imwrite(save_path, each_dsn * 255)

    def makedir(self, path):
        if not os.path.exists(path):
            os.mkdir(path)

    def cfg_checker(self, cfg):
        return cfg

    def test_ms(self,
                param_path=r'../ckpt/standard01/log/RCF_vgg16_bn_bsds_pascal_Adam_savemodel_Feb14_10-33-52/epoch_12.pth',
                mode='ms'):
        # load model parameters
        print('parameters:{}'.format(param_path.split('/')[-2:]))
        pre = torch.load(param_path)  # , map_location=torch.device('cpu'))
        self.model.load_state_dict(pre)

        # set models mode equals 'eval'
        self.model.eval()

        print('---------Test MS----------')

        # Forward
        for ind, item in enumerate(self.data_test_loader):
            (data, img_filename) = item
            data = data.cuda()
            print(img_filename)

            img = data.cpu().numpy().squeeze()
            height, width = data.shape[2:]
            if mode == 'ms':
                scale_list = [0.5, 1.0, 1.5]  # 1.5
            elif mode == 's':
                scale_list = [1.0]
            else:
                raise Exception('Not valid mode! Must in s or ms.')

            # save multi-scale output
            dsn1_ms = torch.zeros([1, 1, height, width]).cuda()
            dsn2_ms = torch.zeros([1, 1, height, width]).cuda()
            dsn3_ms = torch.zeros([1, 1, height, width]).cuda()
            dsn4_ms = torch.zeros([1, 1, height, width]).cuda()
            dsn5_ms = torch.zeros([1, 1, height, width]).cuda()
            dsn6_ms = torch.zeros([1, 1, height, width]).cuda()
            dsn7_ms = torch.zeros([1, 1, height, width]).cuda()
            ms_list = [dsn1_ms, dsn2_ms, dsn3_ms, dsn4_ms, dsn5_ms, dsn6_ms, dsn7_ms]

            for scl in scale_list:
                print('------------- scale:{} -------------, data max:{}, data min:{}'.format(scl, torch.max(data),
                                                                                              torch.min(data)))
                img_scale = cv2.resize(img.transpose((1, 2, 0)), (0, 0), fx=scl, fy=scl, interpolation=cv2.INTER_LINEAR)
                data_ms = torch.from_numpy(img_scale.transpose((2, 0, 1))).float().unsqueeze(0)

                dsn_list = [i for i in self.model(data_ms.cuda())]
                length = len(dsn_list)
                # dsn1, dsn2, dsn3, dsn4, dsn5, dsn6, dsn7 = self.model(data_ms.cuda()) #
                # dsn_list = [dsn1, dsn2, dsn3, dsn4, dsn5, dsn6, dsn7] #, dsn7

                # get prediction normalized
                if self.cfg.MODEL.loss_func_logits:
                    for i in range(length):
                        dsn_list[i] = torch.sigmoid(dsn_list[i])

                # for i in range(0, 6):
                for i in range(0, length):
                    dsn_np = dsn_list[i].squeeze().cpu().data.numpy()
                    dsn_resize = cv2.resize(dsn_np, (width, height), interpolation=cv2.INTER_LINEAR)
                    dsn_t = torch.from_numpy(dsn_resize).cuda()
                    ms_list[i] += dsn_t / len(scale_list)

            if len(dsn_list) != 7:
                dsn7_ms = torch.zeros([1, 1, height, width]).cuda()
                fuse_weight = [1 / 5, 1 / 5, 1 / 5, 1 / 5, 1 / 5]
                print('fuse weight:\n{}'.format(fuse_weight))
                for i, wi in zip(range(5), fuse_weight):
                    dsn7_ms += ms_list[i] * wi

                ms_list[-1] = dsn7_ms

            # print('weight: ')
            # print(self.model.new_score_weighting.weight)
            # print(self.model.new_score_weighting.bias)  # is changing

            self.save_mat(ms_list, img_filename, 0)

