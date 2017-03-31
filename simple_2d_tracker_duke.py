#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
from __future__ import division

import argparse
from os.path import join as pjoin
from os import makedirs
import time, datetime

# the usual suspects
import numpy as np
import matplotlib as mpl
#mpl.use('Agg')
#mpl.use('GTK')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from mpl_toolkits.axes_grid1 import ImageGrid
from scipy.linalg import block_diag, inv
from scipy.io import loadmat

#tracker stuff
import lib
from simple_track_duke import Track
import h5py
from scipy.spatial.distance import euclidean,mahalanobis
from munkres import Munkres, print_matrix

SEQ_FPS = 60.0
SEQ_DT = 1./SEQ_FPS
SEQ_SHAPE = (1080, 1920)
STATE_SHAPE = (270, 480)
HOT_CMAP = lib.get_transparent_colormap()
NUM_CAMS = 8 # which cam to consider (from 1 to NUM_CAMS), max: 8



g_frames = 0  # Global counter for correct FPS in all cases

from collections import Counter

def n_active_tracks(tracklist):
    return '{:2d} +{:2d} +{:2d} ={:2d}'.format(
        sum(t.status == 'matched' for t in tracklist),
        sum(t.status == 'missed' for t in tracklist),
        sum(t.status == 'init' for t in tracklist),
        len(tracklist),
    )
    #return str(Counter(t.status for t in tracklist).most_common())


def shall_vis(args, curr_frame):
    return args.vis and (curr_frame - args.t0) % args.vis == 0


#@profile
def main(args):
    eval_path = pjoin(args.outdir, 'results/run_{:%Y-%m-%d_%H:%M:%S}.txt'.format(datetime.datetime.now()))
    if args.debug:
        debug_dir = pjoin(args.outdir, 'debug/run_{:%Y-%m-%d_%H:%M:%S}'.format(datetime.datetime.now()))
        makedirs(pjoin(debug_dir, 'crops'), exist_ok=True)
    else:
        debug_dir = None

    track_lists = [[], [], [], [], [], [], [], []]
    already_tracked_gids = [[], [], [], [], [], [], [], []]
    track_id = 1
    det_lists = read_detections()
    gt_list = load_trainval(pjoin(args.basedir, 'ground_truth/trainval.mat'),time_range=[127720, 187540]) #train_val_mini
    DIST_THRESH = 200
    DET_INIT_THRESH = 0.3
    DET_CONTINUE_THRESH = 0.0
    m = Munkres()

    # ===Tracking fun begins: iterate over frames===
    # TODO: global time (duke)
    for curr_frame in range(args.t0, args.t1+1):
        print("\rFrame {}, {} matched/missed/init/total tracks, {} total seen".format(curr_frame, ', '.join(map(n_active_tracks, track_lists)), sum(map(len, track_lists))), end='', flush=True)

        if shall_vis(args, curr_frame):
            images = [plt.imread(pjoin(args.basedir, 'frames-0.5/camera{}/{}.jpg'.format(icam, lib.glob2loc(curr_frame, icam)))) for icam in range(1,NUM_CAMS+1)]


        for icam in range(1, NUM_CAMS+1):
            dist_matrix = []

            curr_dets_idc = np.where(det_lists[icam-1][:,1] == lib.glob2loc(curr_frame, icam))[0]
            curr_dets = det_lists[icam-1][curr_dets_idc]
            curr_dets = curr_dets[curr_dets[:,-1] > DET_CONTINUE_THRESH]

            gt_curr_frame = lib.slice_all(gt_list, gt_list['GFIDs'] == curr_frame)
            curr_gts = lib.slice_all(gt_curr_frame, gt_curr_frame['Cams'] == icam)


            # ===visualization===
            # First, plot what data we have before doing anything.
            if shall_vis(args, curr_frame):
                curr_image = images[icam-1]

                fig, axes = plt.subplots(2, 2, sharex=True, sharey=True, figsize=(20, 12))
                (ax_tl, ax_tr), (ax_bl, ax_br) = axes
                axes = axes.flatten()

                for ax in axes:
                    ax.imshow(curr_image, extent=[0, 1920, 1080, 0])

                # plot (active) tracks
                ax_tl.set_title('Groundtruth')
                ax_tr.set_title('Filtered Groundtruth')
                ax_bl.set_title('Thresholded Detections')
                ax_br.set_title('All Tracks')

                for det in curr_dets:
                    ax_bl.add_patch(patches.Rectangle((det[2], det[3]), det[4] - det[2], det[5] - det[3],
                                                      fill=False, linewidth=det[-1] + 1.5, edgecolor="red"))

                for tid, box in zip(curr_gts['TIDs'], curr_gts['boxes']):
                    vis_box = lib.box_rel2abs(box)
                    ax_tl.add_patch(patches.Rectangle((vis_box[0], vis_box[1]), vis_box[2], vis_box[3],
                                                      fill=False, linewidth=2.0, edgecolor="blue"))
            # ===/visualization===


            num_curr_dets = len(curr_dets)
            for each_tracker in track_lists[icam-1]:
                # ---PREDICT---
                each_tracker.track_predict()

                # no detections? no distance matrix
                if not num_curr_dets:
                    break

                # ---BUILD DISTANCE MATRIX---
                #  TODO: IoU (outsource distance measure)
                #              #dist_matrix = [euclidean(tracker.x[0::2],curr_dets[i][2:4]) for i in range(len(curr_dets))]
                #inv_P = inv(each_tracker.KF.P[::2,::2])
                dist_matrix_line = np.array([euclidean(each_tracker.KF.x[::2],
                                            (curr_dets[i][2] + (curr_dets[i][4] - curr_dets[i][2]) / 2.,
                                             curr_dets[i][3] + (curr_dets[i][5] - curr_dets[i][3]) / 2.)) for i in range(len(curr_dets))])
                #              #dist_matrix_line = np.array([mahalanobis(each_tracker.KF.x[::2],
                #                                (curr_dets[i][2]+curr_dets[i][4]/2.,
                #                                 curr_dets[i][3]+curr_dets[i][5]/2.),
                #                                inv_P) for i in range(len(curr_dets))])
                #  apply the threshold here (munkres apparently can't deal 100% with inf, so use 999999)
                #              dist_matrix_line[np.where(dist_matrix_line>dist_thresh)] = 999999
                #              dist_matrix.append(dist_matrix_line.tolist())
                dist_matrix_line[np.where(dist_matrix_line > DIST_THRESH)] = 999999
                dist_matrix.append(dist_matrix_line.tolist())

            # Do the Munkres! (Hungarian algo) to find best matching tracks<->dets
            # at first, all detections (if any) are unassigend
            unassigned_dets = set(range(num_curr_dets))
            if len(dist_matrix) != 0:
                nn_indexes = m.compute(dist_matrix)
                # perform update step for each match (check for threshold, to see, if it's actually a miss)
                for nn_match_idx in range(len(nn_indexes)):
                    # ---UPDATE---
                    if (dist_matrix[nn_indexes[nn_match_idx][0]][nn_indexes[nn_match_idx][1]] <= DIST_THRESH):
                        nn_det = curr_dets[nn_indexes[nn_match_idx][1]]  # 1st: track_idx, 2nd: 0=track_idx, 1 det_idx
                        track_lists[icam-1][nn_indexes[nn_match_idx][0]].track_update([nn_det[2] + (nn_det[4] - nn_det[2]) / 2., nn_det[3] + (nn_det[5] - nn_det[3])/2.])
                        track_lists[icam-1][nn_indexes[nn_match_idx][0]].track_is_matched(curr_frame)
                        # remove detection from being unassigend
                        unassigned_dets.remove(nn_indexes[nn_match_idx][1])
                    else:
                        track_lists[icam-1][nn_indexes[nn_match_idx][0]].track_is_missed(curr_frame)
                # set tracks without any match to miss
                for miss_idx in list(set(range(len(track_lists[icam-1]))) - set([i[0] for i in nn_indexes])):
                    track_lists[icam-1][miss_idx].track_is_missed(curr_frame)



            if not args.gt_init:
                ### B) 1: get new tracks from unassigned detections
                for unassigend_det_idx in unassigned_dets:
                    if curr_dets[unassigend_det_idx][-1] > DET_INIT_THRESH:
                        init_pose = [curr_dets[unassigend_det_idx][2] + (curr_dets[unassigend_det_idx][4] - curr_dets[unassigend_det_idx][2]) / 2.,
                                     curr_dets[unassigend_det_idx][3] + (curr_dets[unassigend_det_idx][5] - curr_dets[unassigend_det_idx][3]) / 2.]
                        new_track = Track(SEQ_DT, curr_frame, init_pose, track_id=track_id)
                        track_id = track_id + 1
                        track_lists[icam-1].append(new_track)
            else:
                ### B) 2: new tracks from (unassigend) ground truth
                for tid,box in zip(curr_gts['TIDs'],curr_gts['boxes']):
                    if tid in already_tracked_gids[icam-1]:
                        continue
                    # l t w h
                    abs_box = lib.box_rel2abs(box)
                    new_track = Track(SEQ_DT, curr_frame, lib.box_center_xy(abs_box), track_id=tid,
                                      init_thresh=3,delete_thresh=5)
                    track_lists[icam - 1].append(new_track)
                    already_tracked_gids[icam-1].append(tid)

                    if shall_vis(args, curr_frame):
                        ax_tr.add_patch(patches.Rectangle((abs_box[0], abs_box[1]), abs_box[2], abs_box[3],
                                                          fill=False, linewidth=2.0, edgecolor="lime"))

            ### C) further track-management
            # delete tracks marked as 'deleted' in this tracking cycle
            track_lists[icam-1] = [i for i in track_lists[icam-1] if i.status != 'deleted']

            # ===visualization===
            ### Plot the current state of tracks.
            if shall_vis(args, curr_frame):
                for tracker in track_lists[icam-1]:
                    tracker.plot_track(ax_br, plot_past_trajectory=True)
                    # plt.gca().add_patch(patches.Rectangle((tracker.KF.x[0]-50, tracker.KF.x[2]-200), 100, 200,
                    #                                       fill=False, linewidth=3, edgecolor=tracker.color))

                for ax in axes:
                    ax.set_adjustable('box-forced')
                    ax.set_xlim(0, 1920)
                    ax.set_ylim(1080, 0)

                # plt.imshow(curr_heatmap,alpha=0.5,interpolation='none',cmap='hot',extent=[0,curr_image.shape[1],curr_image.shape[0],0],clim=(0, 10))
                # savefig(pjoin(args.outdir, 'camera{}/res_img_{:06d}.jpg'.format(icam, curr_frame)), quality=80)
                fig.savefig(pjoin(args.outdir, 'camera{}/res_img_{:06d}.jpg'.format(icam, curr_frame)),
                            quality=80, bbox_inches='tight', pad_inches=0.2)
                # plt.show()
                # fig.close()
                plt.close()


        # ==evaluation===
        if True:
            with open(eval_path, 'a') as eval_file:
                for icam0, track_list in enumerate(track_lists):
                    for tracker in track_list:
                        track_eval_line = tracker.get_track_eval_line(cid=icam0 + 1,frame=curr_frame)
                        if track_eval_line is not None:
                            eval_file.write('{} {} {} {} {} {} {} {} {}\n'.format(*track_eval_line))

        global g_frames
        g_frames += 1


# Heavily adapted and fixed from http://robotics.usc.edu/~ampereir/wordpress/?p=626
def savefig(fname, fig=None, orig_size=None, **kw):
    if fig is None:
        fig = plt.gcf()
    fig.patch.set_alpha(0)

    w, h = fig.get_size_inches()
    if orig_size is not None:  # Aspect ratio scaling if required
        fw, fh = w, h
        w, h = orig_size
        fig.set_size_inches((fw, (fw/w)*h))
        fig.set_dpi((fw/w)*fig.get_dpi())

    ax = fig.gca()
    ax.set_frame_on(False)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_axis_off()
    #ax.set_xlim(0, w); ax.set_ylim(h, 0)
    fig.savefig(fname, transparent=True, bbox_inches='tight', pad_inches=0, **kw)

def read_detections():
    print("Reading detections...")
    det_list = [[], [], [], [], [], [], [], []]
    for icam in range(1,NUM_CAMS+1):
        print("Camera {}...".format(icam))
        fname = pjoin(args.basedir, 'detections/camera{}_trainval-mini.mat'.format(icam))
        try:
            det_list[icam - 1] = loadmat(fname)['detections']
        except NotImplementedError:
            with h5py.File(fname, 'r') as det_file:
                det_list[icam - 1] = np.array(det_file['detections']).T
        # ===setup list of all detections (dukeMTMC format)===
        #with h5py.File(fname, 'r') as det_file:
        #    det_list[icam - 1] = np.array(det_file['detections']).T
        print("done!")
    return det_list


def slice_all(f, s):
    return {k: v[s] for k,v in f.items()}

def load_trainval(fname, time_range=[49700, 227540]):
    try:
        m = loadmat(fname)['trainData']
    except NotImplementedError:
        with h5py.File(fname, 'r') as f:
            m = np.array(f['trainData']).T

    data = {
        'Cams': np.array(m[:,0], dtype=int),
        'TIDs': np.array(m[:,1], dtype=int),
        'LFIDs': np.array(m[:,2], dtype=int),
        'boxes': np.array(m[:,3:7], dtype=float),
        'world': np.array(m[:,7:9]),
        'feet': np.array(m[:,9:]),
    }

    # boxes are l t w h
    data['boxes'][:,0] /= 1920
    data['boxes'][:,1] /= 1080
    data['boxes'][:,2] /= 1920
    data['boxes'][:,3] /= 1080

    # Compute global frame numbers once.
    start_times = [5543, 3607, 27244, 31182, 1, 22402, 18968, 46766]
    data['GFIDs'] = np.array(data['LFIDs'])
    for icam, t0 in zip(range(1,9), start_times):
        data['GFIDs'][data['Cams'] == icam] += t0 - 1

    #return data
    return slice_all(data, (time_range[0] <= data['GFIDs']) & (data['GFIDs'] <= time_range[1]))


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='2D tracker test.')
    parser.add_argument('--basedir', nargs='?', default='/work/breuers/dukeMTMC/',
                        help='Path to `train` folder of 2DMOT2015.')
    parser.add_argument('--outdir', nargs='?', default='/work/breuers/dukeMTMC/results/',
                        help='Where to store generated output. Only needed if `--vis` is also passed.')
    parser.add_argument('--model', default='lunet2',
                        help='Name of the model to load. Corresponds to module names in lib/models. Or `fake`')
    #parser.add_argument('--weights', default='/work/breuers/dukeMTMC/models/lunet2-final.pkl',
    parser.add_argument('--weights', default='/work/breuers/dukeMTMC/models/lunet2-combined-024000.pkl',
                        help='Name of the weights to load for the model (path to .pkl file).')
    parser.add_argument('--t0', default=49700, type=int,
                        help='Time of first frame.')
    parser.add_argument('--t1', default=227540, type=int,
                        help='Time of last frame, inclusive.')
    parser.add_argument('--vis', default=0, type=int,
                        help='Generate and save visualization of the results, every X frame.')
    parser.add_argument('--debug', action='store_true',
                        help='Generate extra many debugging outputs (in outdir).')
    parser.add_argument('--gt_init', action='store_true',
                        help='Use first groundtruth to init tracks.')
    args = parser.parse_args()
    print(args)

    # Prepare output dirs
    for icam in range(1, NUM_CAMS+1):
        makedirs(pjoin(args.outdir, 'camera{}'.format(icam)), exist_ok=True)
    makedirs(pjoin(args.outdir, 'results'), exist_ok=True)

    tstart = time.time()
    try:
        main(args)
    except KeyboardInterrupt:
        print()

    print('FPS: {:.3f}'.format(g_frames / (time.time() - tstart)))
