# Copyright (c) 2017-present, Facebook, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##############################################################################

# Example usage:
# data_loader_benchmark.par \
#   NUM_GPUS 2 \
#   TRAIN.DATASETS "('voc_2007_trainval',)" \
#   TRAIN.PROPOSAL_FILES /path/to/voc_2007_trainval/proposals.pkl \
#   DATA_LOADER.NUM_THREADS 4 \
#   DATA_LOADER.MINIBATCH_QUEUE_SIZE 64 \
#   DATA_LOADER.BLOBS_QUEUE_CAPACITY 8

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import argparse
import logging
import pprint
import sys
import time

import numpy as np
from caffe2.python import core, muji, workspace

from detectron.core.config import (assert_and_infer_cfg, cfg,
                                   merge_cfg_from_file, merge_cfg_from_list)
from detectron.datasets.roidb import combined_roidb_for_training
from detectron.roi_data.loader import RoIDataLoader
from detectron.utils.logging import setup_logging
from detectron.utils.timer import Timer


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--num-batches",
        dest="num_batches",
        help="Number of minibatches to run",
        default=200,
        type=int,
    )
    parser.add_argument(
        "--sleep",
        dest="sleep_time",
        help="Seconds sleep to emulate a network running",
        default=0.1,
        type=float,
    )
    parser.add_argument("--cfg",
                        dest="cfg_file",
                        help="optional config file",
                        default=None,
                        type=str)
    parser.add_argument(
        "--x-factor",
        dest="x_factor",
        help="simulates x-factor more GPUs",
        default=1,
        type=int,
    )
    parser.add_argument(
        "--profiler",
        dest="profiler",
        help="profile minibatch load time",
        action="store_true",
    )
    parser.add_argument(
        "opts",
        help="See detectron/core/config.py for all options",
        default=None,
        nargs=argparse.REMAINDER,
    )
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    args = parser.parse_args()
    return args


def loader_loop(roi_data_loader):
    load_timer = Timer()
    iters = 100
    for i in range(iters):
        load_timer.tic()
        roi_data_loader.get_next_minibatch()
        load_timer.toc()
        print("{:d}/{:d}: Average get_next_minibatch time: {:.3f}s".format(
            i + 1, iters, load_timer.average_time))


def main(opts):
    logger = logging.getLogger(__name__)
    roidb = combined_roidb_for_training(cfg.TRAIN.DATASETS,
                                        cfg.TRAIN.PROPOSAL_FILES)
    logger.info("{:d} roidb entries".format(len(roidb)))
    roi_data_loader = RoIDataLoader(
        roidb,
        num_loaders=cfg.DATA_LOADER.NUM_THREADS,
        minibatch_queue_size=cfg.DATA_LOADER.MINIBATCH_QUEUE_SIZE,
        blobs_queue_capacity=cfg.DATA_LOADER.BLOBS_QUEUE_CAPACITY,
    )
    blob_names = roi_data_loader.get_output_names()

    net = core.Net("dequeue_net")
    net.type = "dag"
    all_blobs = []
    for gpu_id in range(cfg.NUM_GPUS):
        with core.NameScope("gpu_{}".format(gpu_id)):
            with core.DeviceScope(muji.OnGPU(gpu_id)):
                for blob_name in blob_names:
                    blob = core.ScopedName(blob_name)
                    all_blobs.append(blob)
                    workspace.CreateBlob(blob)
                    logger.info("Creating blob: {}".format(blob))
                net.DequeueBlobs(roi_data_loader._blobs_queue_name, blob_names)
    logger.info("Protobuf:\n" + str(net.Proto()))

    if opts.profiler:
        import cProfile

        cProfile.runctx("loader_loop(roi_data_loader)",
                        globals(),
                        locals(),
                        sort="cumulative")
    else:
        loader_loop(roi_data_loader)

    roi_data_loader.register_sigint_handler()
    roi_data_loader.start(prefill=True)
    total_time = 0
    for i in range(opts.num_batches):
        start_t = time.time()
        for _ in range(opts.x_factor):
            workspace.RunNetOnce(net)
        total_time += (time.time() - start_t) / opts.x_factor
        logger.info(
            "{:d}/{:d}: Averge dequeue time: {:.3f}s  [{:d}/{:d}]".format(
                i + 1,
                opts.num_batches,
                total_time / (i + 1),
                roi_data_loader._minibatch_queue.qsize(),
                cfg.DATA_LOADER.MINIBATCH_QUEUE_SIZE,
            ))
        # Sleep to simulate the time taken by running a little network
        time.sleep(opts.sleep_time)
        # To inspect:
        # blobs = workspace.FetchBlobs(all_blobs)
        # from IPython import embed; embed()
    logger.info("Shutting down data loader...")
    roi_data_loader.shutdown()


if __name__ == "__main__":
    workspace.GlobalInit(["caffe2", "--caffe2_log_level=0"])
    logger = setup_logging(__name__)
    logger.setLevel(logging.DEBUG)
    logging.getLogger("detectron.roi_data.loader").setLevel(logging.INFO)
    np.random.seed(cfg.RNG_SEED)
    args = parse_args()
    logger.info("Called with args:")
    logger.info(args)
    if args.cfg_file is not None:
        merge_cfg_from_file(args.cfg_file)
    if args.opts is not None:
        merge_cfg_from_list(args.opts)
    assert_and_infer_cfg()
    logger.info("Running with config:")
    logger.info(pprint.pformat(cfg))
    main(args)
