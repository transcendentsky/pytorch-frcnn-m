# coding:utf-8
# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Xinlei Chen
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

import utils.timer

from layer_utils.snippets import generate_anchors_pre
from layer_utils.proposal_layer import proposal_layer
from layer_utils.proposal_top_layer import proposal_top_layer
from layer_utils.anchor_target_layer import anchor_target_layer
from layer_utils.proposal_target_layer import proposal_target_layer
from utils.visualization import draw_bounding_boxes

from layer_utils.roi_pooling.roi_pool import RoIPoolFunction
from layer_utils.roi_align.crop_and_resize import CropAndResizeFunction

from model.config import cfg, tmp_lam, tmp_lam2, tprint

import tensorboardX as tb

from scipy.misc import imresize


class DropBlock2DMix(nn.Module):
    """
    DropBlock with mixing
    """
    def __init__(self, drop_prob, block_size, test=False):
        super(DropBlock2DMix, self).__init__()
        print("[*] using Dropblock mix")
        print("[***]  Setting fixed drop_window")
        self.drop_prob = drop_prob
        self.block_size = block_size
        self.test = test

    def forward(self, x, index):
        # shape: (bsize, channels, height, width)

        assert x.dim() == 4, \
            "Expected input with 4 dimensions (bsize, channels, height, width)"

        if not self.training or self.drop_prob == 0.:
            # raise ValueError("Dropblock mix, drop_prob > 0 ?")
            return x, None
        else:
            # sample from a mask
            mask_reduction = self.block_size // 2
            mask_height = x.shape[-2] - mask_reduction
            mask_width = x.shape[-1] - mask_reduction
            mask_sizes = [mask_height, mask_width]

            if any([x <= 0 for x in mask_sizes]):
                raise ValueError('Input of shape {} is too small for block_size {}'
                                 .format(tuple(x.shape), self.block_size))

            # get gamma value
            # gamma = self._compute_gamma(x, mask_sizes)
            # if self.test: print("--- gamma ---\n", gamma)
            # # sample mask
            # mask = Bernoulli(gamma).sample((x.shape[0], *mask_sizes))
            # if self.test: print("---  mask ---\n", mask)
            bs = x.shape[0]
            hw = mask_width
            rads = torch.randint(0, hw * hw, (bs,)).long()
            rads = torch.unsqueeze(rads, 1)
            mask = torch.zeros(bs, hw*hw).scatter_(1, rads, 1).reshape((bs,hw,hw))

            # place mask on input device
            mask = mask.to(x.device)   # mask.cuda()

            # compute block mask
            block_mask = self._compute_block_mask(mask)
            if self.test: print("--- block mask ---\n", block_mask)

            # apply block mask
            # out = x * block_mask[:, None, :, :]

            # if True:
            #     batch_size = x.size()[0]
            #     index = torch.randperm(batch_size).cuda()

            verse_mask = torch.ones_like(block_mask) - block_mask
            if self.test: print("--- verse_mask ---", verse_mask)

            out = x * block_mask[:, None, :, :] + x[index, :] * verse_mask[:, None, :, :] #* 0.1 这里需注意，是否加0.1
            # if self.test: out = x * block_mask[:, None, :, :] + x[index, :] * verse_mask[:, None, :, :] * 0.1
            # scale output
            # out = out * block_mask.numel() / block_mask.sum()

            return out, index

    def _compute_block_mask(self, mask):
        block_mask = F.conv2d(mask[:, None, :, :],
                              torch.ones((1, 1, self.block_size, self.block_size)).to(
                                  mask.device),
                              padding=int(np.ceil(self.block_size // 2) + 1))

        delta = self.block_size // 2
        input_height = mask.shape[-2] + delta
        input_width = mask.shape[-1] + delta

        height_to_crop = block_mask.shape[-2] - input_height
        width_to_crop = block_mask.shape[-1] - input_width

        if height_to_crop != 0:
            block_mask = block_mask[:, :, :-height_to_crop, :]

        if width_to_crop != 0:
            block_mask = block_mask[:, :, :, :-width_to_crop]

        block_mask = (block_mask >= 1).to(device=block_mask.device, dtype=block_mask.dtype)
        block_mask = 1 - block_mask.squeeze(1)

        return block_mask

    def _compute_gamma(self, x, mask_sizes):
        feat_area = x.shape[-2] * x.shape[-1]
        mask_area = mask_sizes[-2] * mask_sizes[-1]
        return (self.drop_prob / (self.block_size ** 2)) * (feat_area / mask_area)


class Network(nn.Module):
    def __init__(self):
        nn.Module.__init__(self)
        self._predictions = {}
        self._losses = {}
        self._anchor_targets = {}
        self._proposal_targets = {}
        self._layers = {}
        self._gt_image = None
        self._act_summaries = {}
        self._score_summaries = {}
        self._event_summaries = {}
        self._image_gt_summaries = {}
        self._variables_to_fix = {}
        self._device = 'cuda'
        self.lam = 0  # Add for mix
        self.mix_training = cfg.MIX_TRAINING
        self.index = None
        self.dbmix = DropBlock2DMix(drop_prob=0.1, block_size=3); print("[*] Using DBMix")
        if self.mix_training:
            print("Using Mix-training for RPN")
            if cfg.RPN_MIX_ONLY:
                print("RPN Mix-training ONLY ... ")
            assert cfg.RCNN_MIX == False, "Only one mix-training can be applied, Runing RPN-MIX ..."
        if cfg.RCNN_MIX:
            print("Using Mis-training for RCNN ing...")
            self.rcnn_mix_idx = None
            assert cfg.MIX_TRAINING == False, "Only one mix-training can be applied, Runing RCNN-MIX ..."
            cfg.RPN_MIX_ONLY = False
            assert cfg.RPN_MIX_ONLY == False, "cfg.RPN_MIX_ONLY should be False, Runing RCNN-MIX ..."

    def _add_gt_image(self):
        # add back mean
        image = self._image_gt_summaries['image'] + cfg.PIXEL_MEANS
        image = imresize(image[0], self._im_info[:2] / self._im_info[2])
        # BGR to RGB (opencv uses BGR)
        self._gt_image = image[np.newaxis, :, :, ::-1].copy(order='C')

    def _add_gt_image_summary(self):
        # use a customized visualization function to visualize the boxes
        self._add_gt_image()
        image = draw_bounding_boxes( \
            self._gt_image, self._image_gt_summaries['gt_boxes'], self._image_gt_summaries['im_info'])

        return tb.summary.image('GROUND_TRUTH', image[0].astype('float32') / 255.0)

    def _add_act_summary(self, key, tensor):
        return tb.summary.histogram('ACT/' + key + '/activations', tensor.data.cpu().numpy(), bins='auto'),
        tb.summary.scalar('ACT/' + key + '/zero_fraction',
                          (tensor.data == 0).float().sum() / tensor.numel())

    def _add_score_summary(self, key, tensor):
        return tb.summary.histogram('SCORE/' + key + '/scores', tensor.data.cpu().numpy(), bins='auto')

    def _add_train_summary(self, key, var):
        return tb.summary.histogram('TRAIN/' + key, var.data.cpu().numpy(), bins='auto')

    def _proposal_top_layer(self, rpn_cls_prob, rpn_bbox_pred):
        rois, rpn_scores = proposal_top_layer( \
            rpn_cls_prob, rpn_bbox_pred, self._im_info,
            self._feat_stride, self._anchors, self._num_anchors)
        return rois, rpn_scores

    def _proposal_layer(self, rpn_cls_prob, rpn_bbox_pred):
        rois, rpn_scores = proposal_layer( \
            rpn_cls_prob, rpn_bbox_pred, self._im_info, self._mode,
            self._feat_stride, self._anchors, self._num_anchors)

        return rois, rpn_scores

    def _roi_pool_layer(self, bottom, rois):
        return RoIPoolFunction(cfg.POOLING_SIZE, cfg.POOLING_SIZE, 1. / 16.)(bottom, rois)

    def _crop_pool_layer(self, bottom, rois, max_pool=True):
        # implement it using stn
        # box to affine
        # input (x1,y1,x2,y2)
        """
        [  x2-x1             x1 + x2 - W + 1  ]
        [  -----      0      ---------------  ]
        [  W - 1                  W - 1       ]
        [                                     ]
        [           y2-y1    y1 + y2 - H + 1  ]
        [    0      -----    ---------------  ]
        [           H - 1         H - 1      ]
        """
        rois = rois.detach()

        x1 = rois[:, 1::4] / 16.0
        y1 = rois[:, 2::4] / 16.0
        x2 = rois[:, 3::4] / 16.0
        y2 = rois[:, 4::4] / 16.0

        height = bottom.size(2)
        width = bottom.size(3)

        pre_pool_size = cfg.POOLING_SIZE * 2 if max_pool else cfg.POOLING_SIZE
        crops = CropAndResizeFunction(pre_pool_size, pre_pool_size)(bottom,
                                                                    torch.cat([y1 / (height - 1), x1 / (width - 1),
                                                                               y2 / (height - 1), x2 / (width - 1)], 1),
                                                                    rois[:, 0].int())

        if max_pool:
            crops = F.max_pool2d(crops, 2, 2)
        return crops

    def _anchor_target_layer(self, rpn_cls_score):
        rpn_labels, rpn_bbox_targets, rpn_bbox_inside_weights, rpn_bbox_outside_weights = \
            anchor_target_layer(
                rpn_cls_score.data, self._gt_boxes.data.cpu().numpy(), self._im_info, self._feat_stride,
                self._anchors.data.cpu().numpy(), self._num_anchors)

        rpn_labels = torch.from_numpy(rpn_labels).float().to(self._device)  # .set_shape([1, 1, None, None])
        rpn_bbox_targets = torch.from_numpy(rpn_bbox_targets).float().to(
            self._device)  # .set_shape([1, None, None, self._num_anchors * 4])
        rpn_bbox_inside_weights = torch.from_numpy(rpn_bbox_inside_weights).float().to(
            self._device)  # .set_shape([1, None, None, self._num_anchors * 4])
        rpn_bbox_outside_weights = torch.from_numpy(rpn_bbox_outside_weights).float().to(
            self._device)  # .set_shape([1, None, None, self._num_anchors * 4])

        rpn_labels = rpn_labels.long()
        self._anchor_targets['rpn_labels'] = rpn_labels
        self._anchor_targets['rpn_bbox_targets'] = rpn_bbox_targets
        self._anchor_targets['rpn_bbox_inside_weights'] = rpn_bbox_inside_weights
        self._anchor_targets['rpn_bbox_outside_weights'] = rpn_bbox_outside_weights

        for k in self._anchor_targets.keys():
            self._score_summaries[k] = self._anchor_targets[k]

        if cfg.MIX_TRAINING:
            rpn_labels2, rpn_bbox_targets2, rpn_bbox_inside_weights2, rpn_bbox_outside_weights2 = \
                anchor_target_layer(
                    rpn_cls_score.data, self._gt_boxes2.data.cpu().numpy(), self._im_info, self._feat_stride,
                    self._anchors.data.cpu().numpy(), self._num_anchors)

            rpn_labels2 = torch.from_numpy(rpn_labels2).float().to(self._device)  # .set_shape([1, 1, None, None])
            rpn_bbox_targets2 = torch.from_numpy(rpn_bbox_targets2).float().to(
                self._device)  # .set_shape([1, None, None, self._num_anchors * 4])
            rpn_bbox_inside_weights2 = torch.from_numpy(rpn_bbox_inside_weights2).float().to(
                self._device)  # .set_shape([1, None, None, self._num_anchors * 4])
            rpn_bbox_outside_weights2 = torch.from_numpy(rpn_bbox_outside_weights2).float().to(
                self._device)  # .set_shape([1, None, None, self._num_anchors * 4])

            rpn_labels2 = rpn_labels2.long()
            self._anchor_targets['rpn_labels2'] = rpn_labels2
            self._anchor_targets['rpn_bbox_targets2'] = rpn_bbox_targets2
            self._anchor_targets['rpn_bbox_inside_weights2'] = rpn_bbox_inside_weights2
            self._anchor_targets['rpn_bbox_outside_weights2'] = rpn_bbox_outside_weights2

        return rpn_labels

    def _proposal_target_layer(self, rois, roi_scores):
        rois, roi_scores, labels, bbox_targets, bbox_inside_weights, bbox_outside_weights = \
            proposal_target_layer(
                rois, roi_scores, self._gt_boxes, self._num_classes)

        self._proposal_targets['rois'] = rois
        self._proposal_targets['labels'] = labels.long()
        self._proposal_targets['bbox_targets'] = bbox_targets
        self._proposal_targets['bbox_inside_weights'] = bbox_inside_weights
        self._proposal_targets['bbox_outside_weights'] = bbox_outside_weights

        for k in self._proposal_targets.keys():
            self._score_summaries[k] = self._proposal_targets[k]

        return rois, roi_scores

    def _anchor_component(self, height, width):
        # just to get the shape right
        # height = int(math.ceil(self._im_info.data[0, 0] / self._feat_stride[0]))
        # width = int(math.ceil(self._im_info.data[0, 1] / self._feat_stride[0]))
        anchors, anchor_length = generate_anchors_pre( \
            height, width,
            self._feat_stride, self._anchor_scales, self._anchor_ratios)
        self._anchors = torch.from_numpy(anchors).to(self._device)
        self._anchor_length = anchor_length

    def _smooth_l1_loss(self, bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights, sigma=1.0, dim=[1]):
        sigma_2 = sigma ** 2
        box_diff = bbox_pred - bbox_targets
        in_box_diff = bbox_inside_weights * box_diff
        abs_in_box_diff = torch.abs(in_box_diff)
        smoothL1_sign = (abs_in_box_diff < 1. / sigma_2).detach().float()
        in_loss_box = torch.pow(in_box_diff, 2) * (sigma_2 / 2.) * smoothL1_sign \
                      + (abs_in_box_diff - (0.5 / sigma_2)) * (1. - smoothL1_sign)
        out_loss_box = bbox_outside_weights * in_loss_box
        loss_box = out_loss_box
        for i in sorted(dim, reverse=True):
            loss_box = loss_box.sum(i)
        loss_box = loss_box.mean()
        return loss_box

    def _add_losses(self, sigma_rpn=3.0):
        if self.mix_training == False:

            # RPN, class loss
            rpn_cls_score = self._predictions['rpn_cls_score_reshape'].view(-1, 2)
            rpn_label = self._anchor_targets['rpn_labels'].view(-1)
            rpn_select = (rpn_label.data != -1).nonzero().view(-1)
            rpn_cls_score = rpn_cls_score.index_select(0, rpn_select).contiguous().view(-1, 2)
            rpn_label = rpn_label.index_select(0, rpn_select).contiguous().view(-1)
            rpn_cross_entropy = F.cross_entropy(rpn_cls_score, rpn_label)

            # RPN, bbox loss
            rpn_bbox_pred = self._predictions['rpn_bbox_pred']
            rpn_bbox_targets = self._anchor_targets['rpn_bbox_targets']
            rpn_bbox_inside_weights = self._anchor_targets['rpn_bbox_inside_weights']
            rpn_bbox_outside_weights = self._anchor_targets['rpn_bbox_outside_weights']
            rpn_loss_box = self._smooth_l1_loss(rpn_bbox_pred, rpn_bbox_targets, rpn_bbox_inside_weights,
                                                rpn_bbox_outside_weights, sigma=sigma_rpn, dim=[1, 2, 3])
        else:
            # RPN, class loss
            if cfg.MIX_TEST:
                print("WARNING: Just for TEST CODE")
                rpn_cls_score = self._predictions['rpn_cls_score_reshape'].view(-1, 2)
                rpn_label2 = self._anchor_targets['rpn_labels2'].view(-1)
                rpn_select2 = (rpn_label2.data != -1).nonzero().view(-1)
                rpn_cls_score2 = rpn_cls_score.index_select(0, rpn_select2).contiguous().view(-1, 2)
                rpn_label2 = rpn_label2.index_select(0, rpn_select2).contiguous().view(-1)
                rpn_cross_entropy2 = F.cross_entropy(rpn_cls_score2, rpn_label2)
                rpn_cross_entropy = rpn_cross_entropy2

                rpn_bbox_pred = self._predictions['rpn_bbox_pred']
                rpn_bbox_targets2 = self._anchor_targets['rpn_bbox_targets2']
                rpn_bbox_inside_weights2 = self._anchor_targets['rpn_bbox_inside_weights2']
                rpn_bbox_outside_weights2 = self._anchor_targets['rpn_bbox_outside_weights2']
                rpn_loss_box2 = self._smooth_l1_loss(rpn_bbox_pred, rpn_bbox_targets2, rpn_bbox_inside_weights2,
                                                     rpn_bbox_outside_weights2, sigma=sigma_rpn, dim=[1, 2, 3])
                rpn_loss_box = rpn_loss_box2
            else:
                rpn_cls_score = self._predictions['rpn_cls_score_reshape'].view(-1, 2)
                rpn_label = self._anchor_targets['rpn_labels'].view(-1)
                rpn_select = (rpn_label.data != -1).nonzero().view(-1)
                rpn_cls_score1 = rpn_cls_score.index_select(0, rpn_select).contiguous().view(-1, 2)
                rpn_label = rpn_label.index_select(0, rpn_select).contiguous().view(-1)
                rpn_cross_entropy1 = F.cross_entropy(rpn_cls_score1, rpn_label)
                ###
                rpn_label2 = self._anchor_targets['rpn_labels2'].view(-1)
                rpn_select2 = (rpn_label2.data != -1).nonzero().view(-1)
                rpn_cls_score2 = rpn_cls_score.index_select(0, rpn_select2).contiguous().view(-1, 2)
                rpn_label2 = rpn_label2.index_select(0, rpn_select2).contiguous().view(-1)
                rpn_cross_entropy2 = F.cross_entropy(rpn_cls_score2, rpn_label2)

                rpn_cross_entropy = tmp_lam * rpn_cross_entropy1 + (1 - tmp_lam) * rpn_cross_entropy2

                # RPN, bbox loss
                rpn_bbox_pred = self._predictions['rpn_bbox_pred']

                rpn_bbox_targets = self._anchor_targets['rpn_bbox_targets']
                rpn_bbox_inside_weights = self._anchor_targets['rpn_bbox_inside_weights']
                rpn_bbox_outside_weights = self._anchor_targets['rpn_bbox_outside_weights']
                rpn_loss_box1 = self._smooth_l1_loss(rpn_bbox_pred, rpn_bbox_targets, rpn_bbox_inside_weights,
                                                     rpn_bbox_outside_weights, sigma=sigma_rpn, dim=[1, 2, 3])

                rpn_bbox_targets2 = self._anchor_targets['rpn_bbox_targets2']
                rpn_bbox_inside_weights2 = self._anchor_targets['rpn_bbox_inside_weights2']
                rpn_bbox_outside_weights2 = self._anchor_targets['rpn_bbox_outside_weights2']
                rpn_loss_box2 = self._smooth_l1_loss(rpn_bbox_pred, rpn_bbox_targets2, rpn_bbox_inside_weights2,
                                                     rpn_bbox_outside_weights2, sigma=sigma_rpn, dim=[1, 2, 3])

                rpn_loss_box = tmp_lam * rpn_loss_box1 + (1 - tmp_lam) * rpn_loss_box2

        if cfg.RPN_MIX_ONLY == False:
            # RCNN, class loss
            cls_score = self._predictions["cls_score"]
            label = self._proposal_targets["labels"].view(-1)
            cross_entropy = F.cross_entropy(cls_score.view(-1, self._num_classes), label)

            # RCNN, bbox loss
            bbox_pred = self._predictions['bbox_pred']
            bbox_targets = self._proposal_targets['bbox_targets']
            bbox_inside_weights = self._proposal_targets['bbox_inside_weights']
            bbox_outside_weights = self._proposal_targets['bbox_outside_weights']
            loss_box = self._smooth_l1_loss(bbox_pred, bbox_targets, bbox_inside_weights, bbox_outside_weights)

            if cfg.RCNN_MIX:
                label2 = self._proposal_targets["labels"][self.rcnn_mix_idx, :].view(-1)
                cross_entropy2 = F.cross_entropy(cls_score.view(-1, self._num_classes), label2)
                cross_entropy = tmp_lam2 * cross_entropy + (1 - tmp_lam2) * cross_entropy2

                bbox_targets2 = self._proposal_targets['bbox_targets'][self.rcnn_mix_idx, :]
                bbox_inside_weights2 = self._proposal_targets['bbox_inside_weights'][self.rcnn_mix_idx, :]
                bbox_outside_weights2 = self._proposal_targets['bbox_outside_weights'][self.rcnn_mix_idx, :]
                loss_box2 = self._smooth_l1_loss(bbox_pred, bbox_targets2, bbox_inside_weights2, bbox_outside_weights2)
                loss_box = tmp_lam2 * loss_box + (1 - tmp_lam2) * loss_box2
        else:
            tprint("WARNING: RPN_MIX_ONLY")

        if cfg.RPN_MIX_ONLY:
            self._losses['rpn_cross_entropy'] = rpn_cross_entropy
            self._losses['rpn_loss_box'] = rpn_loss_box
            loss = rpn_cross_entropy + rpn_loss_box
        else:
            if cfg.RCNN_MIX:
                self._losses['cross_entropy'] = cross_entropy
                self._losses['loss_box'] = loss_box
                loss = cross_entropy + loss_box
            else:
                self._losses['cross_entropy'] = cross_entropy
                self._losses['loss_box'] = loss_box
                self._losses['rpn_cross_entropy'] = rpn_cross_entropy
                self._losses['rpn_loss_box'] = rpn_loss_box
                loss = cross_entropy + loss_box + rpn_cross_entropy + rpn_loss_box

        self._losses['total_loss'] = loss

        # for k in self._losses.keys():
        #   self._event_summaries[k] = self._losses[k]

        return loss

    def _region_proposal(self, net_conv):
        rpn = F.relu(self.rpn_net(net_conv))
        self._act_summaries['rpn'] = rpn

        rpn_cls_score = self.rpn_cls_score_net(rpn)  # batch * (num_anchors * 2) * h * w

        # change it so that the score has 2 as its channel size
        rpn_cls_score_reshape = rpn_cls_score.view(1, 2, -1,
                                                   rpn_cls_score.size()[-1])  # batch * 2 * (num_anchors*h) * w
        rpn_cls_prob_reshape = F.softmax(rpn_cls_score_reshape, dim=1)

        # Move channel to the last dimenstion, to fit the input of python functions
        rpn_cls_prob = rpn_cls_prob_reshape.view_as(rpn_cls_score).permute(0, 2, 3,
                                                                           1)  # batch * h * w * (num_anchors * 2)
        rpn_cls_score = rpn_cls_score.permute(0, 2, 3, 1)  # batch * h * w * (num_anchors * 2)
        rpn_cls_score_reshape = rpn_cls_score_reshape.permute(0, 2, 3,
                                                              1).contiguous()  # batch * (num_anchors*h) * w * 2
        rpn_cls_pred = torch.max(rpn_cls_score_reshape.view(-1, 2), 1)[1]

        rpn_bbox_pred = self.rpn_bbox_pred_net(rpn)
        rpn_bbox_pred = rpn_bbox_pred.permute(0, 2, 3, 1).contiguous()  # batch * h * w * (num_anchors*4)

        if self._mode == 'TRAIN':
            rois, roi_scores = self._proposal_layer(rpn_cls_prob, rpn_bbox_pred)  # rois, roi_scores are varible
            rpn_labels = self._anchor_target_layer(rpn_cls_score)
            rois, _ = self._proposal_target_layer(rois, roi_scores)
        else:
            if cfg.TEST.MODE == 'nms':
                rois, _ = self._proposal_layer(rpn_cls_prob, rpn_bbox_pred)
            elif cfg.TEST.MODE == 'top':
                rois, _ = self._proposal_top_layer(rpn_cls_prob, rpn_bbox_pred)
            else:
                raise NotImplementedError

        self._predictions["rpn_cls_score"] = rpn_cls_score
        self._predictions["rpn_cls_score_reshape"] = rpn_cls_score_reshape
        self._predictions["rpn_cls_prob"] = rpn_cls_prob
        self._predictions["rpn_cls_pred"] = rpn_cls_pred
        self._predictions["rpn_bbox_pred"] = rpn_bbox_pred
        self._predictions["rois"] = rois

        return rois

    def _region_classification(self, fc7):
        cls_score = self.cls_score_net(fc7)
        cls_pred = torch.max(cls_score, 1)[1]
        cls_prob = F.softmax(cls_score, dim=1)
        bbox_pred = self.bbox_pred_net(fc7)

        self._predictions["cls_score"] = cls_score
        self._predictions["cls_pred"] = cls_pred
        self._predictions["cls_prob"] = cls_prob
        self._predictions["bbox_pred"] = bbox_pred

        return cls_prob, bbox_pred

    def _image_to_head(self):
        raise NotImplementedError

    def _head_to_tail(self, pool5):
        raise NotImplementedError

    def create_architecture(self, num_classes, tag=None,
                            anchor_scales=(8, 16, 32), anchor_ratios=(0.5, 1, 2)):
        self._tag = tag

        self._num_classes = num_classes
        self._anchor_scales = anchor_scales
        self._num_scales = len(anchor_scales)

        self._anchor_ratios = anchor_ratios
        self._num_ratios = len(anchor_ratios)

        self._num_anchors = self._num_scales * self._num_ratios

        assert tag != None

        # Initialize layers
        self._init_modules()

    def _init_modules(self):
        self._init_head_tail()

        # rpn
        self.rpn_net = nn.Conv2d(self._net_conv_channels, cfg.RPN_CHANNELS, [3, 3], padding=1)

        self.rpn_cls_score_net = nn.Conv2d(cfg.RPN_CHANNELS, self._num_anchors * 2, [1, 1])

        self.rpn_bbox_pred_net = nn.Conv2d(cfg.RPN_CHANNELS, self._num_anchors * 4, [1, 1])

        self.cls_score_net = nn.Linear(self._fc7_channels, self._num_classes)
        self.bbox_pred_net = nn.Linear(self._fc7_channels, self._num_classes * 4)

        self.init_weights()

    def _run_summary_op(self, val=False):
        """
        Run the summary operator: feed the placeholders with corresponding newtork outputs(activations)
        """
        summaries = []
        # Add image gt
        summaries.append(self._add_gt_image_summary())
        # Add event_summaries
        for key, var in self._event_summaries.items():
            summaries.append(tb.summary.scalar(key, var.item()))
        self._event_summaries = {}
        if not val:
            # Add score summaries
            for key, var in self._score_summaries.items():
                summaries.append(self._add_score_summary(key, var))
            self._score_summaries = {}
            # Add act summaries
            for key, var in self._act_summaries.items():
                summaries += self._add_act_summary(key, var)
            self._act_summaries = {}
            # Add train summaries
            for k, var in dict(self.named_parameters()).items():
                if var.requires_grad:
                    summaries.append(self._add_train_summary(k, var))

            self._image_gt_summaries = {}

        return summaries

    def _predict(self):
        # This is just _build_network in tf-faster-rcnn
        torch.backends.cudnn.benchmark = False
        net_conv = self._image_to_head()

        # build the anchors for the image
        self._anchor_component(net_conv.size(2), net_conv.size(3))

        # RPN layer forward
        rois = self._region_proposal(net_conv)

        if cfg.RPN_MIX_ONLY == False:  # IF RPN Only, skip this block.
            if cfg.POOLING_MODE == 'crop':
                pool5 = self._crop_pool_layer(net_conv, rois)
            else:
                pool5 = self._roi_pool_layer(net_conv, rois)
            # RCNN-MIX
            tprint("pool5", pool5.size()[0])
            if cfg.RCNN_MIX:
                pool5 = pool5.detach()
                _len = pool5.size()[0]
                # ##  mixup
                lam = np.random.beta(0.1, 0.1)
                tmp_lam2 = lam
                rcnn_index = np.arange(_len)
                np.random.shuffle(rcnn_index)
                self.rcnn_mix_idx = rcnn_index
                pool5 = tmp_lam2 * pool5 + (1 - tmp_lam2) * pool5[rcnn_index, :]
            if True:
                pool5= self.dbmix(pool5, rcnn_index)
            if self._mode == 'TRAIN':
                torch.backends.cudnn.benchmark = True  # benchmark because now the input size are fixed
            fc7 = self._head_to_tail(pool5)

            cls_prob, bbox_pred = self._region_classification(fc7)
            return rois, cls_prob, bbox_pred
        else:
            return rois, None, None
        # for k in self._predictions.keys():
        #   self._score_summaries[k] = self._predictions[k]

    def forward(self, image, im_info, gt_boxes=None, gt_boxes2=None, mode='TRAIN'):

        ### RPN_mix holder ???
        self._image_gt_summaries['image'] = image
        self._image_gt_summaries['gt_boxes'] = gt_boxes
        self._image_gt_summaries['im_info'] = im_info

        # mix-training
        self._image = torch.from_numpy(image.transpose([0, 3, 1, 2])).to(self._device)
        self._im_info = im_info  # No need to change; actually it can be an list
        self._gt_boxes = torch.from_numpy(gt_boxes).to(self._device) if gt_boxes is not None else None
        self._gt_boxes2 = torch.from_numpy(gt_boxes2).to(self._device) if gt_boxes2 is not None else None
        if cfg.MIX_TEST:
            assert self._gt_boxes == None, "TEST: GT_BOXES 1 is NONE "
            assert self._gt_boxes2 == None, "TEST: GT_BOXES 2 is NONE "

        self._mode = mode

        rois, cls_prob, bbox_pred = self._predict()

        if mode == 'TEST':
            stds = bbox_pred.data.new(cfg.TRAIN.BBOX_NORMALIZE_STDS).repeat(self._num_classes).unsqueeze(0).expand_as(
                bbox_pred)
            means = bbox_pred.data.new(cfg.TRAIN.BBOX_NORMALIZE_MEANS).repeat(self._num_classes).unsqueeze(0).expand_as(
                bbox_pred)
            self._predictions["bbox_pred"] = bbox_pred.mul(stds).add(means)
        else:
            self._add_losses()  # compute losses

    def init_weights(self):
        def normal_init(m, mean, stddev, truncated=False):
            """
            weight initalizer: truncated normal and random normal.
            """
            # x is a parameter
            if truncated:
                m.weight.data.normal_().fmod_(2).mul_(stddev).add_(mean)  # not a perfect approximation
            else:
                m.weight.data.normal_(mean, stddev)
            m.bias.data.zero_()

        normal_init(self.rpn_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.rpn_cls_score_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.rpn_bbox_pred_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.cls_score_net, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.bbox_pred_net, 0, 0.001, cfg.TRAIN.TRUNCATED)

    # Extract the head feature maps, for example for vgg16 it is conv5_3
    # only useful during testing mode
    def extract_head(self, image):
        feat = self._layers["head"](torch.from_numpy(image.transpose([0, 3, 1, 2])).to(self._device))
        return feat

    # only useful during testing mode
    def test_image(self, image, im_info):
        self.eval()
        with torch.no_grad():
            self.forward(image, im_info, None, mode='TEST')
        cls_score, cls_prob, bbox_pred, rois = self._predictions["cls_score"].data.cpu().numpy(), \
                                               self._predictions['cls_prob'].data.cpu().numpy(), \
                                               self._predictions['bbox_pred'].data.cpu().numpy(), \
                                               self._predictions['rois'].data.cpu().numpy()
        return cls_score, cls_prob, bbox_pred, rois

    def delete_intermediate_states(self):
        # Delete intermediate result to save memory
        for d in [self._losses, self._predictions, self._anchor_targets, self._proposal_targets]:
            for k in list(d):
                del d[k]

    def get_summary(self, blobs):
        self.eval()
        self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'])
        # self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'], blobs['gt_boxes2'])
        self.train()
        summary = self._run_summary_op(True)

        return summary

    def train_step(self, blobs, train_op):
        self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'], blobs['gt_boxes2'])
        if cfg.RPN_MIX_ONLY:
            rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].item(), \
                                                                   self._losses['rpn_loss_box'].item(), \
                                                                   -1, -1, \
                                                                   self._losses['total_loss'].item()
        else:
            rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].item(), \
                                                                   self._losses['rpn_loss_box'].item(), \
                                                                   self._losses['cross_entropy'].item(), \
                                                                   self._losses['loss_box'].item(), \
                                                                   self._losses['total_loss'].item()
        # utils.timer.timer.tic('backward')
        train_op.zero_grad()
        self._losses['total_loss'].backward()
        # utils.timer.timer.toc('backward')
        train_op.step()

        self.delete_intermediate_states()

        return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss

    def train_step_with_summary(self, blobs, train_op):
        # self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'])
        raise NotImplementedError("[DEBUG] This module is under coding ...")
        self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'], blobs['gt_boxes2'])
        rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss = self._losses["rpn_cross_entropy"].item(), \
                                                               self._losses['rpn_loss_box'].item(), \
                                                               self._losses['cross_entropy'].item(), \
                                                               self._losses['loss_box'].item(), \
                                                               self._losses['total_loss'].item()
        train_op.zero_grad()
        self._losses['total_loss'].backward()
        train_op.step()
        summary = self._run_summary_op()

        self.delete_intermediate_states()

        return rpn_loss_cls, rpn_loss_box, loss_cls, loss_box, loss, summary

    def train_step_no_return(self, blobs, train_op):
        # self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'])
        self.forward(blobs['data'], blobs['im_info'], blobs['gt_boxes'], blobs['gt_boxes2'])
        train_op.zero_grad()
        self._losses['total_loss'].backward()
        train_op.step()
        self.delete_intermediate_states()

    def load_state_dict(self, state_dict):
        """
        Because we remove the definition of fc layer in resnet now, it will fail when loading
        the model trained before.
        To provide back compatibility, we overwrite the load_state_dict
        """
        nn.Module.load_state_dict(self, {k: state_dict[k] for k in list(self.state_dict())})
