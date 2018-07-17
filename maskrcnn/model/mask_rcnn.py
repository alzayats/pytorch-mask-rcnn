import math
import re

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from maskrcnn.roialign.crop_and_resize.crop_and_resize import CropAndResizeAligned
from .utils import not_empty, is_empty, box_refinement, SamePad2d, concatenate_detections, flatten_detections,\
    unflatten_detections, split_detections, torch_tensor_to_int_list
from .rpn import compute_rpn_losses, compute_rpn_losses_per_sample, alt_forward_method
from .rcnn import FasterRCNNBaseModel, pyramid_roi_align, compute_rcnn_bbox_loss,\
    compute_rcnn_class_loss, bbox_overlaps



############################################################
#  Mask head
############################################################

class MaskHead (nn.Module):
    """Mask head model.

    config: configuration object
    depth: number of channels per feature map pixel incoming from FPN
    pool_size: size of output extracted by ROI-align for mask generation
    num_classes: number of object classes
    roi_canonical_scale: the natural size of objects detected at the canonical FPN pyramid level
    roi_canonical_level: the index identifying the canonical FPN pyramid level
    min_pyramid_level: the index of the lowest FPN pyramid level
    max_pyramid_level: the index of the highest FPN pyramid level
    roi_align_function: string identifying the ROI-align function used:
        'crop_and_resize': crops the selected region and resizes to `pool_size` using bilinear interpolation
        'border_aware_crop_and_resize': as 'crop_and_resize' except that the feature map pixels are assumed to have
            their centres at '(y+0.5, x+0.5)', so the image extnds from (0,0) to (height, width)
        'roi_align': ROIAlign from Detectron.pytorch
    roi_align_sampling_ratio: sampling ratio for 'roi_align' function

    Invoking this model returns mask predictions mask_pred:
        mask_pred: [batch, detection, cls, mask_height, mask_width] class specific mask predicted probabilities
    """
    def __init__(self, config, depth, pool_size, num_classes, roi_canonical_scale, roi_canonical_level,
                 min_pyramid_level, max_pyramid_level, roi_align_function, roi_align_sampling_ratio):
        super(MaskHead, self).__init__()
        self.depth = depth
        self.pool_size = pool_size
        self.num_classes = num_classes
        self.roi_canonical_scale = roi_canonical_scale
        self.roi_canonical_level = roi_canonical_level
        self.min_pyramid_level = min_pyramid_level
        self.max_pyramid_level = max_pyramid_level
        self.roi_align_function = roi_align_function
        self.roi_align_sampling_ratio = roi_align_sampling_ratio

        if config.TORCH_PADDING:
            self.padding = None
            pad = 1 * config.MASK_CONV_DILATION
            dilation = config.MASK_CONV_DILATION
        else:
            self.padding = SamePad2d(kernel_size=3, stride=1)
            pad = 0
            dilation = 1

        self.conv1 = nn.Conv2d(self.depth, 256, kernel_size=3, stride=1, padding=pad, dilation=dilation)
        self.conv2 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=pad, dilation=dilation)
        self.conv3 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=pad, dilation=dilation)
        self.conv4 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=pad, dilation=dilation)

        if config.MASK_BATCH_NORM:
            self.bn1 = nn.BatchNorm2d(256, eps=config.BN_EPS)
            self.bn2 = nn.BatchNorm2d(256, eps=config.BN_EPS)
            self.bn3 = nn.BatchNorm2d(256, eps=config.BN_EPS)
            self.bn4 = nn.BatchNorm2d(256, eps=config.BN_EPS)
        else:
            self.bn1 = self.bn2 = self.bn3 = self.bn4 = None

        self.deconv = nn.ConvTranspose2d(256, 256, kernel_size=2, stride=2)
        self.conv5 = nn.Conv2d(256, num_classes, kernel_size=1, stride=1)
        self.sigmoid = nn.Sigmoid()
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x, rois, n_rois_per_sample, image_shape):
        x = pyramid_roi_align(x, rois, n_rois_per_sample,
                              self.pool_size, image_shape,
                              self.roi_canonical_scale, self.roi_canonical_level,
                              self.min_pyramid_level, self.max_pyramid_level,
                              self.roi_align_function, self.roi_align_sampling_ratio)

        if self.padding is not None:
            x = self.padding(x)
        x = self.conv1(x)
        if self.bn1 is not None:
            x = self.bn1(x)
        x = self.relu(x)

        if self.padding is not None:
            x = self.padding(x)
        x = self.conv2(x)
        if self.bn2 is not None:
            x = self.bn2(x)
        x = self.relu(x)

        if self.padding is not None:
            x = self.padding(x)
        x = self.conv3(x)
        if self.bn3 is not None:
            x = self.bn3(x)
        x = self.relu(x)

        if self.padding is not None:
            x = self.padding(x)
        x = self.conv4(x)
        if self.bn4 is not None:
            x = self.bn4(x)
        x = self.relu(x)

        x = self.deconv(x)
        x = self.relu(x)
        x = self.conv5(x)
        x = self.sigmoid(x)

        (x,) = unflatten_detections(n_rois_per_sample, x)

        return x

    def detectron_weight_mapping(self):
        det_map = {}
        orphans = []
        det_map['conv1.weight'] = '_[mask]_fcn1_w'
        det_map['conv1.bias'] = '_[mask]_fcn1_b'
        det_map['conv2.weight'] = '_[mask]_fcn2_w'
        det_map['conv2.bias'] = '_[mask]_fcn2_b'
        det_map['conv3.weight'] = '_[mask]_fcn3_w'
        det_map['conv3.bias'] = '_[mask]_fcn3_b'
        det_map['conv4.weight'] = '_[mask]_fcn4_w'
        det_map['conv4.bias'] = '_[mask]_fcn4_b'

        det_map['deconv.weight'] = 'conv5_mask_w'
        det_map['deconv.bias'] = 'conv5_mask_b'

        det_map['conv5.weight'] = 'mask_fcn_logits_w'
        det_map['conv5.bias'] = 'mask_fcn_logits_b'
        return det_map, orphans



############################################################
#  Loss Functions
############################################################

def compute_mrcnn_mask_loss(target_masks, target_class_ids, pred_masks):
    """Mask binary cross-entropy loss for the masks head.

    :param target_masks: [num_rois, mask_height, mask_width]. Mask targets as a float32 tensor of values 0 or 1.
    :param target_class_ids: [num_rois]. Target class IDs.
    :param pred_masks: [num_rois, cls, height, width] Class specific mask predictions.
    :return: loss as a torch scalar
    """
    device = pred_masks.device

    if not_empty(target_class_ids):
        # Only positive ROIs contribute to the loss. And only
        # the class specific mask of each ROI.
        positive_ix = torch.nonzero(target_class_ids > 0)[:, 0]
        positive_class_ids = target_class_ids[positive_ix.data].long()
        indices = torch.stack((positive_ix, positive_class_ids), dim=1)

        # Gather the masks (predicted and true) that contribute to loss
        y_true = target_masks[indices[:,0].data, :, :]
        y_pred = pred_masks[indices[:,0].data, indices[:,1].data, :,: ]

        # Binary cross entropy
        loss = F.binary_cross_entropy(y_pred, y_true)
    else:
        loss = torch.tensor([0], dtype=torch.float, device=device)

    return loss


def compute_maskrcnn_losses(config, rpn_pred_class_logits, rpn_pred_bbox, rpn_target_match, rpn_target_bbox,
                            rpn_target_num_pos_per_sample, rcnn_pred_class_logits, rcnn_pred_bbox_deltas,
                            rcnn_target_class_ids, rcnn_target_deltas, mrcnn_pred_mask, mrcnn_target_mask):
    """Loss for Mask RCNN network

    Combines the RPN, R-CNN and Mask losses.

    The RPN predictions and targets retain their batch/anchor shape.
    The RCNN and mask predictions and targets should be flattened from [batch, detection] into [batch & detection].
    This is done by the `train_forward` method, so these two fit together.

    :param config: configuration object
    :param rpn_pred_class_logits: [batch, anchors, 2] RPN classifier logits for FG/BG if using softmax or focal loss for
        RPN class (see config.RPN_OBJECTNESS_FUNCTION),
        [batch, anchors] RPN classifier FG logits if using sigmoid.
    :param rpn_pred_bbox: [batch, anchors, (dy, dx, log(dh), log(dw))]
    :param rpn_target_match: [batch, anchors]. Anchor match type. 1=positive,
               -1=negative, 0=neutral anchor.
    :param rpn_target_bbox: [batch, max positive anchors, (dy, dx, log(dh), log(dw))].
        Uses 0 padding to fill in unsed bbox deltas.
    :param rpn_target_num_pos_per_sample: [batch] number of positives per sample

    :param rcnn_pred_class_logits: [num_rois, num_classes] Predicted class logits
    :param rcnn_pred_bbox_deltas: [num_rois, num_classes, (dy, dx, log(dh), log(dw))] predicted bbox deltas
    :param rcnn_target_class_ids: [num_rois]. Target class IDs. Uses zero padding to fill in the array.
    :param rcnn_target_deltas: [num_rois, (dy, dx, log(dh), log(dw))] target box deltas

    :param mrcnn_pred_mask: [num_rois, height, width, num_classes] Class specific mask predictions
    :param mrcnn_target_mask: [num_rois, mask_height, mask_width] Mask targets as a float32 tensor of values 0 or 1.

    :return: (rpn_class_loss, rpn_bbox_loss, rcnn_class_loss, rcnn_bbox_loss, mrcnn_mask_loss)
        rpn_class_loss: RPN objectness loss as a torch scalar
        rpn_bbox_loss: RPN box loss as a torch scalar
        rcnn_class_loss: RCNN classification loss as a torch scalar
        rcnn_bbox_loss: RCNN bbox loss as a torch scalar
        mrcnn_mask_loss: Mask-RCNN mask loss as a torch scalar
    """

    rpn_target_num_pos_per_sample = torch_tensor_to_int_list(rpn_target_num_pos_per_sample)

    rpn_class_loss, rpn_bbox_loss = compute_rpn_losses(config, rpn_pred_class_logits, rpn_pred_bbox, rpn_target_match, rpn_target_bbox,
                                                       rpn_target_num_pos_per_sample)

    rcnn_class_loss = compute_rcnn_class_loss(rcnn_target_class_ids, rcnn_pred_class_logits)
    rcnn_bbox_loss = compute_rcnn_bbox_loss(rcnn_target_deltas, rcnn_target_class_ids, rcnn_pred_bbox_deltas)

    mrcnn_mask_loss = compute_mrcnn_mask_loss(mrcnn_target_mask, rcnn_target_class_ids, mrcnn_pred_mask)

    return [rpn_class_loss, rpn_bbox_loss, rcnn_class_loss, rcnn_bbox_loss, mrcnn_mask_loss]



############################################################
#  Detection target generation
############################################################

def _mask_box_enlarge_img_batch(config, box_img):
    """
    Apply mask box enlargement as controlled by the configuration parameters

    :param config: configuration instance
    :param box_img: boxes as a [N, detections, (y1, x1, y2, x2)] tensor in image co-ordinates
    :return: enalarged boxes in image co-ordinates as a [N, detections, (y1, x1, y2, x2)] tensor
    """
    if config.MASK_BOX_ENLARGE != 1.0 or config.MASK_BOX_BORDER_MIN != 0.0:
        # Compute size and centre
        size = box_img[:, :, 2:4] - box_img[:, :, 0:2]
        centre = (box_img[:, :, 2:4] + box_img[:, :, 0:2]) * 0.5

        # Compute enlarged size as the maximum of enlarging by fraction and growing with border
        enlarged_size = torch.max(size * config.MASK_BOX_ENLARGE,
                                  size + config.MASK_BOX_BORDER_MIN * 2.0)

        # Convert to [y1, x1, y2, x2]
        return torch.cat([centre - enlarged_size * 0.5, centre + enlarged_size * 0.5], dim=2), True
    else:
        return box_img, False


def _mask_box_enlarge_nrm_sample(config, box_nrm, nrm_to_img_scale):
    """
    Apply mask box enlargement as controlled by the configuration parameters

    :param config: configuration instance
    :param box_nrm: boxes as a [N, (y1, x1, y2, x2)] tensor in normalized co-ordinates
    :param nrm_to_img_scale: scale factor that will convert normalized co-ordinates to
        image co-ordinates
    :return: enalarged boxes in normalized co-ordinates as a [N, (y1, x1, y2, x2)] tensor
    """
    if config.MASK_BOX_ENLARGE != 1.0 or config.MASK_BOX_BORDER_MIN != 0.0:
        # Transform boxes to image co-ordinates
        box_img = box_nrm * nrm_to_img_scale

        # Compute size and centre
        size = box_img[:, 2:4] - box_img[:, 0:2]
        centre = (box_img[:, 2:4] + box_img[:, 0:2]) * 0.5

        # Compute enlarged size as the maximum of enlarging by fraction and growing with border
        enlarged_size = torch.max(size * config.MASK_BOX_ENLARGE,
                                  size + config.MASK_BOX_BORDER_MIN * 2.0)

        # Convert to [y1, x1, y2, x2]
        enlarged_box_img = torch.cat([centre - enlarged_size * 0.5, centre + enlarged_size * 0.5], dim=1)

        # Convert to normalized co-ordinates
        return enlarged_box_img / nrm_to_img_scale
    else:
        return box_nrm


def maskrcnn_detection_target_one_sample(config, image_size, proposals_nrm, prop_class_logits, prop_class,
                                         prop_bbox_deltas, gt_class_ids, gt_boxes_nrm, gt_masks,
                                         hard_negative_mining=False):
    """Subsamples proposals and matches them with ground truth boxes, generating target box refinement,
    class_ids and masks.

    Works on a single sample.

    If hard_negative_mining is True, values must be provided for prop_class_logits, prop_class and prop_bbox_deltas,
    otherwise they are optional.

    :param config: configuration object
    :param image_size: image size as a `(height, width)` tuple
    :param proposals_nrm: [N, (y1, x1, y2, x2)] in normalized coordinates.
    :param prop_class_logits: (optional) [N, N_CLASSES] predicted RCNN class logits for each proposal (used
        when hard negative mining is enabled).
    :param prop_class: (optional) [N, N_CLASSES] predicted RCNN class probabilities for each proposal (used
        when hard negative mining is enabled).
    :param prop_bbox_deltas: (optional) [N, N_CLASSES, 4] predicted RCNN bbox deltas for each proposal (used
        when hard negative mining is enabled).
    :param gt_class_ids: [N_GT] Ground truth class IDs.
    :param gt_boxes_nrm: [N_GT, (y1, x1, y2, x2)] Ground truth boxes in normalized coordinates.
    :param gt_masks: [height, width, N_GT] of float type
    :param hard_negative_mining: bool; if True, use hard negative mining to choose target boxes

    :return: (rois_nrm, roi_class_logits, roi_class_probs, roi_bbox_deltas, target_class_ids, target_deltas, target_masks)
            Target ROIs and corresponding class IDs, bounding box shifts, where:
        rois_nrm: [RCNN_TRAIN_ROIS_PER_IMAGE, (y1, x1, y2, x2)] proposals selected for training, in normalized coordinates
        roi_class_logits: [RCNN_TRAIN_ROIS_PER_IMAGE, N_CLASSES] predicted class logits of selected proposals
        roi_class_probs: [RCNN_TRAIN_ROIS_PER_IMAGE, N_CLASSES] predicted class probabilities of selected proposals
        roi_bbox_deltas: [RCNN_TRAIN_ROIS_PER_IMAGE, N_CLASSES, 4] predicted bbox deltas of selected proposals.
        target_class_ids: [RCNN_TRAIN_ROIS_PER_IMAGE]. Integer class IDs.
        target_deltas: [RCNN_TRAIN_ROIS_PER_IMAGE, NUM_CLASSES,
                        (dy, dx, log(dh), log(dw), class_id)]
                       Class-specific bbox refinments.
        target_masks: [RCNN_TRAIN_ROIS_PER_IMAGE, height, width)
                 Masks cropped to bbox boundaries and resized to neural network output size.
    """
    device = proposals_nrm.device
    nrm_scale = torch.tensor([image_size[0], image_size[1], image_size[0], image_size[1]], dtype=torch.float,
                             device=device)

    if hard_negative_mining:
        if prop_class_logits is None:
            raise ValueError('prop_class_logits cannot be None when hard_negative_mining is True')
        if prop_class is None:
            raise ValueError('prop_class cannot be None when hard_negative_mining is True')
        if prop_bbox_deltas is None:
            raise ValueError('prop_bbox_deltas cannot be None when hard_negative_mining is True')

    if prop_class_logits is not None and prop_class is not None and prop_bbox_deltas is not None:
        has_rcnn_predictions = True
    elif prop_class_logits is None and prop_class is None and prop_bbox_deltas is None:
        has_rcnn_predictions = False
    else:
        raise ValueError('prop_class_logits, prop_class and prop_bbox_deltas should either all have '
                         'values or all be None')

    # Handle COCO crowds
    # A crowd box in COCO is a bounding box around several instances. Exclude
    # them from training. A crowd box is given a negative class ID.
    if not_empty(torch.nonzero(gt_class_ids < 0)):
        crowd_ix = torch.nonzero(gt_class_ids < 0)[:, 0]
        non_crowd_ix = torch.nonzero(gt_class_ids > 0)[:, 0]
        crowd_boxes = gt_boxes_nrm[crowd_ix.data, :]
        crowd_masks = gt_masks[crowd_ix.data, :, :]
        gt_class_ids = gt_class_ids[non_crowd_ix.data]
        gt_boxes_nrm = gt_boxes_nrm[non_crowd_ix.data, :]
        gt_masks = gt_masks[non_crowd_ix.data, :]

        # Compute overlaps with crowd boxes [anchors, crowds]
        crowd_overlaps = bbox_overlaps(proposals_nrm, crowd_boxes)
        crowd_iou_max = torch.max(crowd_overlaps, dim=1)[0]
        no_crowd_bool = crowd_iou_max < 0.001
    else:
        no_crowd_bool = torch.tensor([True] * proposals_nrm.size()[0], dtype=torch.uint8, device=device)

    # Compute overlaps matrix [proposals, gt_boxes]
    overlaps = bbox_overlaps(proposals_nrm, gt_boxes_nrm)

    # Determine postive and negative ROIs
    roi_iou_max = torch.max(overlaps, dim=1)[0]

    # 1. Positive ROIs are those with >= 0.5 IoU with a GT box
    positive_roi_bool = roi_iou_max >= 0.5

    if hard_negative_mining:
        # Get the probability of the negative (empty) class for each proposal; needed for hard negative mining
        negative_cls_prob = prop_class[:,0]
    else:
        negative_cls_prob = None

    # Subsample ROIs. Aim for 33% positive
    # Positive ROIs
    if not_empty(torch.nonzero(positive_roi_bool)):
        positive_indices = torch.nonzero(positive_roi_bool)[:, 0]

        positive_count = int(config.RCNN_TRAIN_ROIS_PER_IMAGE *
                             config.RCNN_ROI_POSITIVE_RATIO)

        if hard_negative_mining:
            # Hard negative mining
            # Choose samples with the highest negative class (class 0) probability (incorrect)
            if positive_count < positive_indices.size()[0]:
                _, hard_neg_idx = negative_cls_prob[positive_indices.data].topk(positive_count)
                positive_indices = positive_indices[hard_neg_idx]
        else:
            rand_idx = torch.randperm(positive_indices.size()[0])
            rand_idx = rand_idx[:positive_count]
            rand_idx = rand_idx.to(device)
            positive_indices = positive_indices[rand_idx]

        positive_count = positive_indices.size()[0]
        positive_rois = proposals_nrm[positive_indices.data, :]
        if has_rcnn_predictions:
            positive_class_logits = prop_class_logits[positive_indices.data,:]
            positive_class_probs = prop_class[positive_indices.data,:]
            positive_bbox_deltas = prop_bbox_deltas[positive_indices.data,:,:]
        else:
            positive_class_logits = positive_class_probs = positive_bbox_deltas = None

        # Assign positive ROIs to GT boxes.
        positive_overlaps = overlaps[positive_indices.data,:]
        roi_gt_box_assignment = torch.max(positive_overlaps, dim=1)[1]
        roi_gt_boxes = gt_boxes_nrm[roi_gt_box_assignment.data, :]
        roi_gt_class_ids = gt_class_ids[roi_gt_box_assignment.data]

        # Compute bbox refinement for positive ROIs
        deltas = box_refinement(positive_rois.data, roi_gt_boxes.data)
        if config.RCNN_BBOX_USE_STD_DEV:
            std_dev = torch.tensor(config.BBOX_STD_DEV, dtype=torch.float, device=device)
            deltas /= std_dev

        # Assign positive ROIs to GT masks
        roi_masks = gt_masks[roi_gt_box_assignment.data,:,:]

        # Compute mask targets
        mask_boxes = _mask_box_enlarge_nrm_sample(config, positive_rois, nrm_scale)
        if config.USE_MINI_MASK:
            # Transform ROI corrdinates from normalized image space
            # to normalized mini-mask space.
            mask_gt_boxes = _mask_box_enlarge_nrm_sample(config, roi_gt_boxes, nrm_scale)
            y1, x1, y2, x2 = mask_boxes.chunk(4, dim=1)
            gt_y1, gt_x1, gt_y2, gt_x2 = mask_gt_boxes.chunk(4, dim=1)
            gt_h = gt_y2 - gt_y1
            gt_w = gt_x2 - gt_x1
            y1 = (y1 - gt_y1) / gt_h
            x1 = (x1 - gt_x1) / gt_w
            y2 = (y2 - gt_y1) / gt_h
            x2 = (x2 - gt_x1) / gt_w
            mask_boxes = torch.cat([y1, x1, y2, x2], dim=1)
        box_ids = torch.arange(roi_masks.size()[0], dtype=torch.int, device=device)
        masks = CropAndResizeAligned(config.MASK_SHAPE[0], config.MASK_SHAPE[1], 0)(
            roi_masks.detach().unsqueeze(1), mask_boxes.detach(), box_ids.detach())
        masks = masks.squeeze(1)

        # Threshold mask pixels at 0.5 to have GT masks be 0 or 1 to use with
        # binary cross entropy loss.
        masks = torch.round(masks)
    else:
        positive_count = 0

    # 2. Negative ROIs are those with < 0.5 with every GT box. Skip crowds.
    negative_roi_bool = roi_iou_max < 0.5
    negative_roi_bool = negative_roi_bool & no_crowd_bool
    # Negative ROIs. Add enough to maintain positive:negative ratio.
    if not_empty(torch.nonzero(negative_roi_bool)) and positive_count>0:
        negative_indices = torch.nonzero(negative_roi_bool)[:, 0]
        r = 1.0 / config.RCNN_ROI_POSITIVE_RATIO
        negative_count = int(r * positive_count - positive_count)

        if hard_negative_mining:
            # Hard negative mining
            # Choose samples with the lowest negative class (class 0) probability (incorrect)
            if negative_count< negative_indices.size()[0]:
                _, hard_neg_idx = negative_cls_prob[negative_indices.data].topk(negative_count, largest=False)
                negative_indices = negative_indices[hard_neg_idx]
        else:
            rand_idx = torch.randperm(negative_indices.size()[0])
            rand_idx = rand_idx[:negative_count]
            rand_idx = rand_idx.to(device)
            negative_indices = negative_indices[rand_idx]

        negative_count = negative_indices.size()[0]
        negative_rois = proposals_nrm[negative_indices.data, :]
        if has_rcnn_predictions:
            negative_class_logits = prop_class_logits[negative_indices.data,:]
            negative_class_probs = prop_class[negative_indices.data,:]
            negative_bbox_deltas = prop_bbox_deltas[negative_indices.data,:,:]
        else:
            negative_class_logits = negative_class_probs = negative_bbox_deltas = None
    else:
        negative_count = 0

    # Append negative ROIs and pad bbox deltas and masks that
    # are not used for negative ROIs with zeros.
    if positive_count > 0 and negative_count > 0:
        rois = torch.cat((positive_rois, negative_rois), dim=0)
        if has_rcnn_predictions:
            roi_class_logits = torch.cat((positive_class_logits, negative_class_logits), dim=0)
            roi_class_probs = torch.cat((positive_class_probs, negative_class_probs), dim=0)
            roi_bbox_deltas = torch.cat((positive_bbox_deltas, negative_bbox_deltas), dim=0)
        else:
            roi_class_logits = roi_class_probs = roi_bbox_deltas = None

        zeros = torch.zeros(negative_count, dtype=torch.long, device=device)
        roi_gt_class_ids = torch.cat([roi_gt_class_ids, zeros], dim=0)

        zeros = torch.zeros((negative_count,4), device=device)
        deltas = torch.cat([deltas, zeros], dim=0)

        zeros = torch.zeros((negative_count,config.MASK_SHAPE[0],config.MASK_SHAPE[1]), device=device)
        masks = torch.cat([masks, zeros], dim=0)
    elif positive_count > 0:
        rois = positive_rois
        roi_class_logits = positive_class_logits
        roi_class_probs = positive_class_probs
        roi_bbox_deltas = positive_bbox_deltas
    elif negative_count > 0:
        rois = negative_rois
        roi_class_logits = negative_class_logits
        roi_class_probs = negative_class_probs
        roi_bbox_deltas = negative_bbox_deltas

        roi_gt_class_ids = torch.zeros(negative_count, device=device, dtype=torch.long)
        deltas = torch.zeros((negative_count, 4), device=device)
        masks = torch.zeros((negative_count,config.MASK_SHAPE[0],config.MASK_SHAPE[1]), device=device)
    else:
        rois = torch.zeros([0], dtype=torch.float, device=device)
        if has_rcnn_predictions:
            roi_class_logits = torch.zeros([0], dtype=torch.float, device=device)
            roi_class_probs = torch.zeros([0], dtype=torch.float, device=device)
            roi_bbox_deltas = torch.zeros([0], dtype=torch.float, device=device)
        else:
            roi_class_logits = roi_class_probs = roi_bbox_deltas = None
        roi_gt_class_ids = torch.zeros([0], dtype=torch.int, device=device)
        deltas = torch.zeros([0], dtype=torch.float, device=device)
        masks = torch.zeros([0], dtype=torch.float, device=device)

    return rois, roi_class_logits, roi_class_probs, roi_bbox_deltas, roi_gt_class_ids, deltas, masks


def maskrcnn_detection_target_batch(config, image_size, proposals_nrm, prop_class_logits, prop_class, prop_bbox_deltas,
                                    n_proposals_per_sample,
                                    gt_class_ids, gt_boxes_nrm, gt_masks, n_gts_per_sample, hard_negative_mining):
    """Subsamples proposals and generates target box refinement, class_ids and masks for each.

    Works on a mini-batch of samples.

    If hard_negative_mining is True, values must be provided for prop_class_logits, prop_class and prop_bbox_deltas,
    otherwise they are optional.

    :param config: configuration object
    :param image_size: image size as a `(height, width)` tuple
    :param proposals_nrm: [batch, N, (y1, x1, y2, x2)] in normalized coordinates. Dim 1 will
            be zero padded if there are not enough proposals.
    :param prop_class_logits: [batch, N, N_CLASSES] predicted class logits for each proposal. Dim 1 will
            be zero padded if there are not enough proposals. Used when hard negative mining is enabled.
    :param prop_class: [batch, N, N_CLASSES] predicted class probabilities for each proposal. Dim 1 will
            be zero padded if there are not enough proposals. Used when hard negative mining is enabled.
    :param prop_bbox_deltas: [batch, N, N_CLASSES, 4] predicted bbox deltas for each proposal. Dim 1 will
            be zero padded if there are not enough proposals. Used when hard negative mining is enabled.
    :param n_proposals_per_sample: number of proposals per sample; specifies the number of proposals in each
            sample and therefore the amount of zero padding
    :param gt_class_ids: [batch, N_GT] Ground truth class IDs. Dim 1 will be zero padded if there are not
            enough GTs
    :param gt_boxes_nrm: [batch, N_GT, (y1, x1, y2, x2)] in normalized coordinates. Dim 1 will be zero padded
            if there are not enough GTs
    :param gt_masks: [batch, height, width, N_GT] of float type
    :param n_gts_per_sample: number of ground truths per sample; specifies the number of ground truths in each
            sample and therefore the amount of zero padding
    :param hard_negative_mining: bool; if True, use hard negative mining to choose target boxes

    :return: (rois_nrm, roi_class_logits, roi_class_probs, roi_bbox_deltas, target_class_ids, target_deltas,
              target_masks, n_targets_per_sample)
            Target ROIs and corresponding class IDs, bounding box shifts, where:
        rois_nrm: [batch, RCNN_TRAIN_ROIS_PER_IMAGE, (y1, x1, y2, x2)] proposals selected for training, in normalized coordinates
        roi_class_logits: [batch, RCNN_TRAIN_ROIS_PER_IMAGE, N_CLASSES] predicted class logits of selected proposals
        roi_class_probs: [batch, RCNN_TRAIN_ROIS_PER_IMAGE, N_CLASSES] predicted class probabilities of selected proposals
        roi_bbox_deltas: [batch, RCNN_TRAIN_ROIS_PER_IMAGE, N_CLASSES, 4] predicted bbox deltas of selected proposals.
        target_class_ids: [batch, RCNN_TRAIN_ROIS_PER_IMAGE]. Integer class IDs.
        target_deltas: [batch, RCNN_TRAIN_ROIS_PER_IMAGE, NUM_CLASSES,
                        (dy, dx, log(dh), log(dw), class_id)]
                       Class-specific bbox refinments.
        target_masks: [batch, RCNN_TRAIN_ROIS_PER_IMAGE, height, width)
                 Masks cropped to bbox boundaries and resized to neural network output size.
        n_targets_per_sample: number of targets per sample
    """
    if hard_negative_mining:
        if prop_class_logits is None:
            raise ValueError('prop_class_logits cannot be None when hard_negative_mining is True')
        if prop_class is None:
            raise ValueError('prop_class cannot be None when hard_negative_mining is True')
        if prop_bbox_deltas is None:
            raise ValueError('prop_bbox_deltas cannot be None when hard_negative_mining is True')

    if prop_class_logits is not None and prop_class is not None and prop_bbox_deltas is not None:
        has_rcnn_predictions = True
    elif prop_class_logits is None and prop_class is None and prop_bbox_deltas is None:
        has_rcnn_predictions = False
    else:
        raise ValueError('prop_class_logits, prop_class and prop_bbox_deltas should either all have '
                         'values or all be None')

    rois = []
    if has_rcnn_predictions:
        roi_class_logits = []
        roi_class_probs = []
        roi_bbox_deltas = []
    else:
        roi_class_logits = roi_class_probs = roi_bbox_deltas = None
    target_class_ids = []
    target_deltas = []
    target_mask = []
    for sample_i, (n_props, n_gts) in enumerate(zip(n_proposals_per_sample, n_gts_per_sample)):
        sample_roi_class_logits = sample_roi_class_probs = sample_roi_bbox_deltas = None
        if n_props > 0 and n_gts > 0:
            if has_rcnn_predictions:
                sample_prop_class_logits = prop_class_logits[sample_i, :n_props]
                sample_prop_class = prop_class[sample_i, :n_props]
                sample_prop_bbox_deltas = prop_bbox_deltas[sample_i, :n_props]
            else:
                sample_prop_class_logits = sample_prop_class = sample_prop_bbox_deltas = None
            sample_rois, sample_roi_class_logits, sample_roi_class_probs, sample_roi_bbox_deltas, \
                    sample_roi_gt_class_ids, sample_deltas, sample_masks = maskrcnn_detection_target_one_sample(
                            config, image_size, proposals_nrm[sample_i, :n_props], sample_prop_class_logits,
                            sample_prop_class, sample_prop_bbox_deltas, gt_class_ids[sample_i, :n_gts],
                            gt_boxes_nrm[sample_i, :n_gts], gt_masks[sample_i, :n_gts])
            if not_empty(sample_rois):
                sample_rois = sample_rois.unsqueeze(0)
                if has_rcnn_predictions:
                    sample_roi_class_logits = sample_roi_class_logits.unsqueeze(0)
                    sample_roi_class_probs = sample_roi_class_probs.unsqueeze(0)
                    sample_roi_bbox_deltas = sample_roi_bbox_deltas.unsqueeze(0)
                sample_roi_gt_class_ids = sample_roi_gt_class_ids.unsqueeze(0)
                sample_deltas = sample_deltas.unsqueeze(0)
                sample_masks = sample_masks.unsqueeze(0)
        else:
            sample_rois = proposals_nrm.data.new()
            if has_rcnn_predictions:
                sample_roi_class_logits = proposals_nrm.data.new()
                sample_roi_class_probs = proposals_nrm.data.new()
                sample_roi_bbox_deltas = proposals_nrm.data.new()
            sample_roi_gt_class_ids = gt_class_ids.data.new()
            sample_deltas = proposals_nrm.data.new()
            sample_masks = gt_masks.data.new()
        rois.append(sample_rois)
        if has_rcnn_predictions:
            roi_class_logits.append(sample_roi_class_logits)
            roi_class_probs.append(sample_roi_class_probs)
            roi_bbox_deltas.append(sample_roi_bbox_deltas)
        target_class_ids.append(sample_roi_gt_class_ids)
        target_deltas.append(sample_deltas)
        target_mask.append(sample_masks)


    if has_rcnn_predictions:
        (rois, roi_class_logits, roi_class_probs, roi_bbox_deltas, roi_gt_class_ids, deltas, masks), n_dets_per_sample = concatenate_detections(
            rois, roi_class_logits, roi_class_probs, roi_bbox_deltas, target_class_ids, target_deltas, target_mask)
    else:
        (rois, roi_gt_class_ids, deltas, masks), n_dets_per_sample = concatenate_detections(
            rois, target_class_ids, target_deltas, target_mask)

    return rois, roi_class_logits, roi_class_probs, roi_bbox_deltas, roi_gt_class_ids, deltas, masks, n_dets_per_sample


def clip_to_windows_batch(windows, boxes):
    """
    Clip a batch boxes to fit within a batch of image windows

        windows: [N, (y1, x1, y2, x2)]. The windows in the images we want to clip to.
        boxes: [N, detections, (y1, x1, y2, x2)]
    """
    boxes[:, :, 0] = boxes[:, :, 0].max(windows[:, None, 0]).min(windows[:, None, 2])
    boxes[:, :, 1] = boxes[:, :, 1].max(windows[:, None, 1]).min(windows[:, None, 3])
    boxes[:, :, 2] = boxes[:, :, 2].max(windows[:, None, 0]).min(windows[:, None, 2])
    boxes[:, :, 3] = boxes[:, :, 3].max(windows[:, None, 1]).min(windows[:, None, 3])

    return boxes




############################################################
#  Mask R-CNN Model
############################################################

class AbstractMaskRCNNModel (FasterRCNNBaseModel):
    """
    Mask R-CNN abstract model

    Network:
    - Mask head
    - inherits from FasterRCNNBaseModel:
        - feature pyramid network for feature extraction
        - RPN head for proposal generation
        - RCNN head for box generation
    """

    def __init__(self, config):
        """
        config: A Sub-class of the Config class
        """
        super(AbstractMaskRCNNModel, self).__init__(config)

        # FPN Mask
        self.mask = MaskHead(config, 256, config.MASK_POOL_SIZE, config.NUM_CLASSES,
                             config.ROI_CANONICAL_SCALE, config.ROI_CANONICAL_LEVEL,
                             config.ROI_MIN_PYRAMID_LEVEL, config.ROI_MAX_PYRAMID_LEVEL,
                             config.ROI_ALIGN_FUNCTION, config.ROI_ALIGN_SAMPLING_RATIO)


    def _train_forward(self, images, gt_class_ids, gt_boxes, gt_masks, n_gts_per_sample, hard_negative_mining=False):
        """Supervised forward training pass helper

        :param images: Tensor of images
        :param gt_class_ids: ground truth box classes [batch, detection]
        :param gt_boxes: ground truth boxes [batch, detection, (y1, x1, y2, x2)]
        :param gt_masks: ground truth masks [batch, detection, mask_height, mask_width]
        :param n_gts_per_sample: number of ground truth detections per sample [batch]
        :param hard_negative_mining: if True, use hard negative mining to choose samples for training R-CNN head

        :return: (rpn_class_logits, rpn_bbox_deltas, rcnn_target_class_ids, rcnn_pred_logits,
                  rcnn_target_deltas, rcnn_pred_deltas, mrcnn_target_mask, mrcnn_pred_mask,
                  n_targets_per_sample) where:
            rpn_class_logits: [batch, anchor]; predicted class logits from RPN
            rpn_bbox_deltas: [batch, anchor, 4]; predicted bounding box deltas
            rcnn_target_class_ids: [batch, ROI]; RCNN target class IDs
            rcnn_pred_logits: [batch, ROI, cls]; RCNN predicted class logits
            rcnn_target_deltas: [batch, ROI, 4]; RCNN target box deltas
            rcnn_pred_deltas: [batch, ROI, cls, 4]; RCNN predicted box deltas
            mrcnn_target_mask: [batch, ROI, mask_height, mask_width) Mask targets
            mrcnn_pred_mask: [batch, ROI, cls, mask_height, mask_width] class specific mask predicted probabilities
            n_targets_per_sample: [batch] the number of target ROIs in each sample
        """
        device = images.device

        # Get image size
        image_size = images.size()[2:]

        # Compute scale factor for converting normalized co-ordinates to pixel co-ordinates
        h, w = image_size
        scale = torch.tensor(np.array([h, w, h, w]), dtype=torch.float, device=device)

        # Get RPN proposals
        pre_nms_limit =  self.config.RPN_PRE_NMS_LIMIT_TRAIN
        nms_threshold =  self.config.RPN_NMS_THRESHOLD
        proposal_count = self.config.RPN_POST_NMS_ROIS_TRAINING
        rpn_feature_maps, mrcnn_feature_maps, rpn_class_logits, rpn_bbox, rpn_rois, _, n_rois_per_sample = \
            self._feature_maps_rpn_preds_and_roi(images, pre_nms_limit, nms_threshold, proposal_count)

        # Normalize coordinates
        gt_boxes_nrm = gt_boxes / scale

        if hard_negative_mining:
            # Apply RCNN head so that we can do hard negative mining in the detection target layer
            # Network Heads
            # Proposal classifier and BBox regressor heads
            roi_class_logits, roi_class, roi_bbox = self.classifier(
                mrcnn_feature_maps, rpn_rois, n_rois_per_sample, image_size)


            # Generate detection targets
            # Subsamples proposals and generates target outputs for training
            # Note that proposal class IDs, gt_boxes, and gt_masks are zero
            # padded. Equally, returned rois and targets are zero padded.
            rois, mrcnn_class_logits, mrcnn_class, mrcnn_bbox, target_class_ids, target_deltas, target_mask, n_targets_per_sample = \
                maskrcnn_detection_target_batch(self.config, image_size, rpn_rois, roi_class_logits, roi_class,
                                                roi_bbox, n_rois_per_sample, gt_class_ids, gt_boxes_nrm, gt_masks,
                                                n_gts_per_sample, hard_negative_mining)

            if is_empty(rois):
                mrcnn_mask = torch.zeros([0], dtype=torch.float, device=device)
            else:
                # Create masks for detections
                # mrcnn_mask: [batch, detection, cls, mask_height, mask_width]
                mrcnn_mask = self.mask(mrcnn_feature_maps, rois, n_targets_per_sample, image_size)

        else:
            # Generate detection targets
            # Subsamples proposals and generates target outputs for training
            # Note that proposal class IDs, gt_boxes, and gt_masks are zero
            # padded. Equally, returned rois and targets are zero padded.
            rois, _, _, _, target_class_ids, target_deltas, target_mask, n_targets_per_sample = \
                maskrcnn_detection_target_batch(self.config, image_size, rpn_rois, None, None, None,
                                                n_rois_per_sample, gt_class_ids, gt_boxes_nrm,
                                                gt_masks, n_gts_per_sample, hard_negative_mining)


            if max(n_targets_per_sample) == 0:
                mrcnn_class_logits = torch.zeros([0], dtype=torch.float, device=device)
                mrcnn_class = torch.zeros([0], dtype=torch.int, device=device)
                mrcnn_bbox = torch.zeros([0], dtype=torch.float, device=device)
                mrcnn_mask = torch.zeros([0], dtype=torch.float, device=device)
            else:
                # Network Heads
                # Proposal classifier and BBox regressor heads
                mrcnn_class_logits, mrcnn_class, mrcnn_bbox = self.classifier(
                    mrcnn_feature_maps, rois, n_targets_per_sample, image_size)

                # Create masks for detections
                # mrcnn_mask: [batch, detection, cls, mask_height, mask_width]
                mrcnn_mask = self.mask(mrcnn_feature_maps, rois, n_targets_per_sample, image_size)

        return [rpn_class_logits, rpn_bbox, target_class_ids, mrcnn_class_logits,
                target_deltas, mrcnn_bbox, target_mask, mrcnn_mask, n_targets_per_sample]


    @alt_forward_method
    def train_forward(self, images, gt_class_ids, gt_boxes, gt_masks, n_gts_per_sample, hard_negative_mining=False):
        """Supervised forward training pass

        :param images: Tensor of images
        :param gt_class_ids: ground truth box classes [batch, detection]
        :param gt_boxes: ground truth boxes [batch, detection, [y1, x1, y2, x2]
        :param gt_masks: ground truth masks [batch, detection, mask_height, mask_width]
        :param n_gts_per_sample: number of ground truth detections per sample [batch]
        :param hard_negative_mining: if True, use hard negative mining to choose samples for training R-CNN head

        :return: (rpn_class_logits, rpn_bbox_deltas, rcnn_target_class_ids, rcnn_pred_logits,
                  rcnn_target_deltas, rcnn_pred_deltas, n_targets_per_sample) where:
            rpn_class_logits: [batch & ROI]; predicted class logits from RPN
            rpn_bbox_deltas: [batch & ROI, 4]; predicted bounding box deltas
            rcnn_target_class_ids: [batch & TGT]; RCNN target class IDs
            rcnn_pred_logits: [batch & TGT, cls]; RCNN predicted class logits
            rcnn_target_deltas: [batch & TGT, 4]; RCNN target box deltas
            rcnn_pred_deltas: [batch & TGT, cls, 4]; RCNN predicted box deltas
            mrcnn_target_mask: [batch & TGT, mask_height, mask_width) Mask targets
            mrcnn_pred_mask: [batch & TGT, mask_height, mask_width, cls] class specific mask predicted probabilities
            n_targets_per_sample: [batch] the number of targets in each sample
        """
        (rpn_class_logits, rpn_bbox, target_class_ids, mrcnn_class_logits,
            target_deltas, rcnn_bbox, target_mask, mrcnn_mask, n_targets_per_sample) = self._train_forward(
                    images, gt_class_ids, gt_boxes, gt_masks, n_gts_per_sample,
                    hard_negative_mining=hard_negative_mining)

        target_class_ids, mrcnn_class_logits, target_deltas, rcnn_bbox, target_mask, mrcnn_mask = \
            flatten_detections(n_targets_per_sample, target_class_ids, mrcnn_class_logits, target_deltas, rcnn_bbox, target_mask, mrcnn_mask)

        return (rpn_class_logits, rpn_bbox, target_class_ids, mrcnn_class_logits,
                target_deltas, rcnn_bbox, target_mask, mrcnn_mask, n_targets_per_sample)


    @alt_forward_method
    def train_loss_forward(self, images, rpn_target_match, rpn_target_bbox, rpn_num_pos,
                           gt_class_ids, gt_boxes, gt_masks, n_gts_per_sample, hard_negative_mining=False):
        """
        Training forward pass returning per-sample losses.

        :param images: training images
        :param rpn_target_match: [batch, anchors]. Anchor match type. 1=positive,
                   -1=negative, 0=neutral anchor.
        :param rpn_target_bbox: [batch, max positive anchors, (dy, dx, log(dh), log(dw))].
            Uses 0 padding to fill in unsed bbox deltas.
        :param rpn_num_pos: [batch] number of positives per sample
        :param gt_class_ids: ground truth box classes [batch, detection]
        :param gt_boxes: ground truth boxes [batch, detection, [y1, x1, y2, x2]
        :param gt_masks: ground truth masks [batch, detection, mask_height, mask_width]
        :param n_gts_per_sample: number of ground truth detections per sample [batch]
        :param hard_negative_mining: if True, use hard negative mining to choose samples for training R-CNN head

        :return: (rpn_class_losses, rpn_bbox_losses, rcnn_class_losses, rcnn_bbox_losses, mrcnn_mask_losses) where
            rpn_class_losses: [batch] RPN objectness per-sample loss
            rpn_bbox_losses: [batch] RPN box delta per-sample loss
            rcnn_class_losses: [batch] RCNN classification per-sample loss
            rcnn_bbox_losses: [batch] RCNN box delta per-sample loss
            mrcnn_mask_losses: [batch] Mask-RCNN mask per-sample loss
        """
        (rpn_class_logits, rpn_pred_bbox, target_class_ids, rcnn_class_logits,
            target_deltas, rcnn_bbox, target_mask, mrcnn_mask, n_targets_per_sample) = self._train_forward(
                    images, gt_class_ids, gt_boxes, gt_masks, n_gts_per_sample,
                    hard_negative_mining=hard_negative_mining)

        rpn_class_losses, rpn_bbox_losses = compute_rpn_losses_per_sample(
            self.config, rpn_class_logits, rpn_pred_bbox, rpn_target_match, rpn_target_bbox, rpn_num_pos)

        rcnn_class_losses = []
        rcnn_bbox_losses = []
        mrcnn_mask_losses = []
        for sample_i, n_targets in enumerate(n_targets_per_sample):
            if n_targets > 0:
                rcnn_class_loss = compute_rcnn_class_loss(
                    target_class_ids[sample_i, :n_targets], rcnn_class_logits[sample_i, :n_targets])
                rcnn_bbox_loss = compute_rcnn_bbox_loss(
                    target_deltas[sample_i, :n_targets], target_class_ids[sample_i, :n_targets],
                    rcnn_bbox[sample_i, :n_targets])
                mrcnn_mask_loss = compute_mrcnn_mask_loss(
                    target_mask[sample_i, :n_targets], target_class_ids[sample_i, :n_targets],
                    mrcnn_mask[sample_i, :n_targets])
                rcnn_class_losses.append(rcnn_class_loss[None])
                rcnn_bbox_losses.append(rcnn_bbox_loss[None])
                mrcnn_mask_losses.append(mrcnn_mask_loss[None])
            else:
                rcnn_class_losses.append(torch.tensor([0.0], dtype=torch.float, device=images.device))
                rcnn_bbox_losses.append(torch.tensor([0.0], dtype=torch.float, device=images.device))
                mrcnn_mask_losses.append(torch.tensor([0.0], dtype=torch.float, device=images.device))
        rcnn_class_losses = torch.cat(rcnn_class_losses, dim=0)
        rcnn_bbox_losses = torch.cat(rcnn_bbox_losses, dim=0)
        mrcnn_mask_losses = torch.cat(mrcnn_mask_losses, dim=0)

        return (rpn_class_losses, rpn_bbox_losses, rcnn_class_losses, rcnn_bbox_losses, mrcnn_mask_losses)


    def mask_detect_forward(self, image_size, image_windows, mrcnn_feature_maps, det_boxes, det_class_ids, n_dets_per_sample):
        """Runs the mask stage of the detection pipeline.

        :param image_size: image shape as a (height, width) tuple
        :param image_windows: [N, (y1, x1, y2, x2)] in image coordinates. The part of the images
                that contain the image excluding the padding.
        :param mrcnn_feature_maps: per-FPN level feature maps for RCNN;
                list of [batch, feat_chn, lvl_height, lvl_width] tensors
        :param det_boxes: [batch, N, (y1, x1, y2, x2)] detection boxes from RCNN in image coordinates
        :param det_class_ids: [batch, N] detection class IDs from RCNN
        :param n_dets_per_sample: number of detections per sample from RCNN

        :return: (mask_boxes, masks) where
            mask_boxes: (batch, (y1, x1, y2, x2) boxes used for mask predictions; will not be the same
                as the detection boxes passed as the `det_boxes` parameter if mask box enlargement is
                enabled
            masks: [batch, detection, height, width] mask predictions as a torch tensor
        """
        device = det_boxes.device

        if sum(n_dets_per_sample) == 0:
            return torch.zeros([0], dtype=torch.float, device=device), torch.zeros([0], dtype=torch.float, device=device)
        else:
            # Enlarge boxes according to config
            mask_boxes, enlarged = _mask_box_enlarge_img_batch(self.config, det_boxes)
            if enlarged:
                image_windows = torch.tensor(image_windows, dtype=torch.float, device=device)
                mask_boxes = clip_to_windows_batch(image_windows, mask_boxes)
                mask_boxes = torch.round(mask_boxes)

            # Convert boxes to normalized coordinates for mask generation
            h, w = image_size
            scale = torch.tensor(np.array([h, w, h, w]), dtype=torch.float, device=device)
            mask_boxes_nrm = mask_boxes / scale[None, None, :]

            # Generate masks
            mrcnn_mask = []

            # Processing a large number of detections can use significant amounts of memory as the
            # pyramid-roi-align step would need to generate a large number of feature maps
            # to feed into the mask head and the convolutional layers in the mask head add additional load.
            # To reduce this, process at most `self.config.DETECTION_BLOCK_SIZE_INFERENCE` detections
            # per sample at a time.
            for mask_i in range(0, mask_boxes_nrm.size()[1], self.config.DETECTION_BLOCK_SIZE_INFERENCE):
                mask_j = min(mask_i + self.config.DETECTION_BLOCK_SIZE_INFERENCE, mask_boxes_nrm.size()[1])

                n_dets_per_sample_block = [
                    min(mask_j, n_dets) - min(mask_i, n_dets) for n_dets in n_dets_per_sample
                ]

                # The pyramid_roi_align function will trim away unused detection boxes (zero padding),
                # so the mask head won't waste resources.
                # The mask head will also convert the resulting mask predictions to a [sample, detection, ...]
                # shape with zero padding
                mrcnn_mask_block = self.mask(mrcnn_feature_maps, mask_boxes_nrm[:, mask_i:mask_j, ...],
                                             n_dets_per_sample_block, image_size)
                # mrcnn_mask_block: [batch, detection_index, object_class, height, width] with zero padding in dim1

                det_class_ids_block = det_class_ids[:, mask_i:mask_j]

                mrcnn_mask_block = mrcnn_mask_block.detach()

                batch_det = mrcnn_mask_block.shape[0] * mrcnn_mask_block.shape[1]
                masks_bd = mrcnn_mask_block.view(batch_det, *mrcnn_mask_block.shape[2:])
                cls_ids_bd = det_class_ids_block.view(batch_det)
                masks_cls_bd = masks_bd[torch.arange(batch_det, dtype=torch.long), cls_ids_bd, ...]
                mrcnn_mask_block_cls = masks_cls_bd.view(mrcnn_mask_block.shape[0], mrcnn_mask_block.shape[1],
                                                         mrcnn_mask_block.shape[3], mrcnn_mask_block.shape[4])

                # mrcnn_mask_block_cls: [batch, detection_index, mask_height, mask_width]
                mrcnn_mask.append(mrcnn_mask_block_cls)

            mrcnn_mask = torch.cat(mrcnn_mask, dim=1)

            return mask_boxes, mrcnn_mask


    def detect_forward(self, images, image_windows, override_class=None):
        """Runs the detection pipeline and returns the results as torch tensors.

        :param images: tensor of images
        :param image_windows: tensor of image windows where each row is [y1, x1, y2, x2]
        :param override_class: int or None; override class ID to always be this class

        :return: (det_boxes, det_class_ids, det_scores, mrcnn_mask, n_dets_per_sample) where
            det_boxes: [batch, detection, 4] detection boxes
            det_class_ids: [batch, detection] detection class IDs
            roi_scores: [batch, detection] detection confidence scores
            mask_boxes: [batch, detection, 4] mask boxes (will be enlarged versions of det_boxes if
                mask box enlargement is enabled in the configuration, otherwise mask_boxes and det_boxes will
                be the same)
            mrcnn_mask: [batch, detection, height, width] mask detections
            n_dets_per_sample: [batch] number of detections per sample in the batch
        """
        image_size = images.shape[2:]

        rpn_feature_maps, mrcnn_feature_maps, rpn_bbox_deltas, rpn_rois, roi_scores, n_rois_per_sample = self.rpn_detect_forward(
            images)

        # det_boxes: [batch, num_detections, (y1, x1, y2, x2)] in image coordinates
        # det_class_ids: [batch, num_detections]
        # det_scores: [batch, num_detections]
        det_boxes, det_class_ids, det_scores, n_dets_per_sample = self.rcnn_detect_forward(
            image_size, image_windows, mrcnn_feature_maps, rpn_rois, n_rois_per_sample, override_class=override_class)

        # mrcnn_mask: [batch, detection, height, width, cls]
        mask_boxes, mrcnn_mask = self.mask_detect_forward(
            image_size, image_windows, mrcnn_feature_maps, det_boxes, det_class_ids, n_dets_per_sample)

        return det_boxes, det_class_ids, det_scores,mask_boxes,  mrcnn_mask, n_dets_per_sample


    def detect_forward_np(self, images, image_windows, override_class=None):
        """Runs the detection pipeline and returns the results as a list of detection tuples consisting of NumPy arrays

        :param images: tensor of images
        :param image_windows: tensor of image windows where each row is [y1, x1, y2, x2]
        :param override_class: int or None; override class ID to always be this class

        :return: [detection0, detection1, ... detectionN] List of detections, one per sample, where each
                detection is a tuple of:
            det_boxes: [1, detections, [y1, x1, y2, x2]] detection boxes
            det_class_ids: [1, detections] detection class IDs
            det_scores: [1, detections] detection confidence scores
            mask_boxes: [1, detections, [y1, x1, y2, x2]] mask boxes (will be enlarged versions of det_boxes if
                mask box enlargement is enabled in the configuration, otherwise mask_boxes and det_boxes will
                be the same)
            mrcnn_mask: [1, detections, height, width] mask detections
        """
        det_boxes, det_class_ids, det_scores, mask_boxes, mrcnn_mask, n_dets_per_sample = self.detect_forward(
            images, image_windows, override_class=override_class)


        if is_empty(det_boxes) or is_empty(det_class_ids) or is_empty(det_scores):
            # No detections
            n_images = images.shape[0]
            return [(np.zeros((1, 0, 4), dtype=np.float32),
                     np.zeros((1, 0), dtype=int),
                     np.zeros((1, 0), dtype=np.float32),
                     np.zeros((1, 0, 4), dtype=np.float32),
                     np.zeros((1, 0) + tuple(self.config.MASK_SHAPE), dtype=np.float32))
                    for i in range(n_images)]

        # Convert to numpy
        det_boxes_np = det_boxes.data.cpu().numpy()
        det_class_ids_np = det_class_ids.data.cpu().numpy()
        det_scores_np = det_scores.data.cpu().numpy()
        mask_boxes_np = mask_boxes.cpu().numpy()
        mrcnn_mask_np = mrcnn_mask.cpu().numpy()

        return split_detections(n_dets_per_sample, det_boxes_np, det_class_ids_np, det_scores_np, mask_boxes_np,
                                mrcnn_mask_np)
