# Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
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

import argparse
import cProfile
import time
import os
import traceback

import numpy as np
import torch
import torchvision_reader

import paddle
import paddle.fluid as fluid
import paddle.fluid.core as core
import paddle.fluid.profiler as profiler
import paddle.fluid.transpiler.distribute_transpiler as distribute_transpiler

import sys
sys.path.append("..")
from utility import add_arguments, print_arguments
import functools
import models
import utils
from env import dist_env
import reader as imagenet_reader

def is_mp_mode():
    return True if os.getenv("FLAGS_selected_gpus") else False

def nccl2_prepare(args, startup_prog):
    config = fluid.DistributeTranspilerConfig()
    config.mode = "nccl2"
    t = fluid.DistributeTranspiler(config=config)

    envs = args.dist_env

    t.transpile(envs["trainer_id"],
        trainers=','.join(envs["trainer_endpoints"]),
        current_endpoint=envs["current_endpoint"],
        startup_program=startup_prog)

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    add_arg = functools.partial(add_arguments, argparser=parser)
    # yapf: disable
    add_arg('use_gpu',          bool,  True,                 "Whether to use GPU or not.")
    add_arg('total_images',     int,   1281167,              "Training image number.")
    add_arg('num_epochs',       int,   120,                  "number of epochs.")
    add_arg('image_shape',      str,   "3,224,224",          "input image size")
    add_arg('model_save_dir',   str,   "output",             "model save directory")
    add_arg('pretrained_model', str,   None,                 "Whether to use pretrained model.")
    add_arg('checkpoint',       str,   None,                 "Whether to resume checkpoint.")
    add_arg('lr',               float, 0.1,                  "set learning rate.")
    add_arg('lr_strategy',      str,   "piecewise_decay",    "Set the learning rate decay strategy.")
    add_arg('model',            str,   "FastResNet",         "Set the network to use.")
    add_arg('data_dir',         str,   "./data/ILSVRC2012",  "The ImageNet dataset root dir.")
    add_arg('model_category',   str,   "models",             "Whether to use models_name or not, valid value:'models','models_name'" )
    add_arg('fp16',             bool,  False,                "Enable half precision training with fp16." )
    add_arg('scale_loss',       float, 1.0,                  "Scale loss for fp16." )
    # for distributed
    add_arg('start_test_pass',    int,  0,                  "Start test after x passes.")
    add_arg('num_threads',        int,  8,                  "Use num_threads to run the fluid program.")
    add_arg('reduce_strategy',    str,  "allreduce",        "Choose from reduce or allreduce.")
    add_arg('log_period',         int,  30,                  "Print period, defualt is 5.")
    add_arg('memory_optimize',      bool,   True,           "Whether to enable memory optimize.")
    # yapf: enable
    args = parser.parse_args()
    return args

def get_device_num():
    import subprocess
    visible_device = os.getenv('CUDA_VISIBLE_DEVICES')
    if visible_device:
        device_num = len(visible_device.split(','))
    else:
        device_num = subprocess.check_output(
            ['nvidia-smi', '-L']).decode().count('\n')
    return device_num

def linear_lr_decay(lr_values, epochs, bs_values, total_images):
    from paddle.fluid.layers.learning_rate_scheduler import _decay_step_counter
    import paddle.fluid.layers.tensor as tensor
    import math

    with paddle.fluid.default_main_program()._lr_schedule_guard():
        global_step = _decay_step_counter()

        lr = tensor.create_global_var(
            shape=[1],
            value=0.0,
            dtype='float32',
            persistable=True,
            name="learning_rate")
        with fluid.layers.control_flow.Switch() as switch:
            last_steps = 0
            for idx, epoch_bound in enumerate(epochs):
                start_epoch, end_epoch = epoch_bound
                linear_epoch = end_epoch - start_epoch
                start_lr, end_lr = lr_values[idx]
                linear_lr = end_lr - start_lr
                steps = last_steps + linear_epoch * total_images / bs_values[idx]
                with switch.case(global_step < steps):
                    decayed_lr = start_lr + linear_lr * ((global_step - last_steps)* 1.0/(steps - last_steps))
                    last_steps = steps
                    fluid.layers.tensor.assign(decayed_lr, lr)
            last_value_var = tensor.fill_constant(
                shape=[1],
                dtype='float32',
                value=float(lr_values[-1]))
            with switch.default():
                fluid.layers.tensor.assign(last_value_var, lr)

        return lr

def linear_lr_decay_by_epoch(lr_values, epochs, bs_values, total_images):
    from paddle.fluid.layers.learning_rate_scheduler import _decay_step_counter
    import paddle.fluid.layers.tensor as tensor
    import math

    with paddle.fluid.default_main_program()._lr_schedule_guard():
        global_step = _decay_step_counter()

        lr = tensor.create_global_var(
            shape=[1],
            value=0.0,
            dtype='float32',
            persistable=True,
            name="learning_rate")
        with fluid.layers.control_flow.Switch() as switch:
            last_steps = 0
            for idx, epoch_bound in enumerate(epochs):
                start_epoch, end_epoch = epoch_bound
                linear_epoch = end_epoch - start_epoch
                start_lr, end_lr = lr_values[idx]
                linear_lr = end_lr - start_lr
                for epoch_step in xrange(linear_epoch):
                    steps = last_steps + (1 + epoch_step) * total_images / bs_values[idx]
                    boundary_val = tensor.fill_constant(
                        shape=[1],
                        dtype='float32',
                        value=float(steps),
                        force_cpu=True)
                    decayed_lr = start_lr + epoch_step * linear_lr * 1.0 / linear_epoch
                    with switch.case(global_step < boundary_val):
                        value_var = tensor.fill_constant(shape=[1], dtype='float32', value=float(decayed_lr)) 
                        print("steps: [%d], epoch : [%d], decayed_lr: [%f]" % (steps, start_epoch + epoch_step, decayed_lr))
                        fluid.layers.tensor.assign(value_var, lr)
                last_steps = steps
            last_value_var = tensor.fill_constant(
                shape=[1],
                dtype='float32',
                value=float(lr_values[-1]))
            with switch.default():
                fluid.layers.tensor.assign(last_value_var, lr)

        return lr
def test_parallel(exe, test_args, args, test_prog, feeder, bs):
    acc_evaluators = []
    for i in xrange(len(test_args[2])):
        acc_evaluators.append(fluid.metrics.Accuracy())

    to_fetch = [v.name for v in test_args[2]]
    test_reader = test_args[3]
    batch_id = 0
    start_ts = time.time()
    for batch_id, data in enumerate(test_reader()):
        acc_rets = exe.run(fetch_list=to_fetch, feed=feeder.feed(data))
        ret_result = [np.mean(np.array(ret)) for ret in acc_rets]
        print("Test batch: [%d], acc_rets: [%s]" % (batch_id, ret_result))
        for i, e in enumerate(acc_evaluators):
            e.update(
                value=np.array(acc_rets[i]), weight=bs)
    num_samples = batch_id * bs * get_device_num()
    print_train_time(start_ts, time.time(), num_samples, "Test")

    return [e.eval() for e in acc_evaluators]

def test_single(exe, test_args, args, test_prog, feeder, bs):
    test_reader = test_args[3]
    to_fetch = [v.name for v in test_args[2]]
    acc1 = fluid.metrics.Accuracy()
    acc5 = fluid.metrics.Accuracy()
    start_ts = time.time()
    for batch_id, data in enumerate(test_reader()):
        batch_size = len(data[0])
        acc_rets = exe.run(test_prog, fetch_list=to_fetch, feed=feeder.feed(data))
        acc1.update(value=np.array(acc_rets[0]), weight=batch_size)
        acc5.update(value=np.array(acc_rets[1]), weight=batch_size)
        if batch_id % 30 == 0:
            print("Test batch: [%d], acc_rets: [%s]" % (batch_id, acc_rets))

    num_samples = batch_id * bs
    print_train_time(start_ts, time.time(), num_samples, "Test")
    return np.mean(acc1.eval()), np.mean(acc5.eval())

def build_program(args, is_train, main_prog, startup_prog, py_reader_startup_prog, img_size, trn_dir, batch_size, min_scale, rect_val):
    dataloader = None
    if is_train:
        dataloader = torchvision_reader.train(traindir=os.path.join(args.data_dir, trn_dir, "train"), bs=batch_size if is_mp_mode() else batch_size * get_device_num(), sz=img_size, min_scale=min_scale)
    else:
        dataloader = torchvision_reader.test(valdir=os.path.join(args.data_dir, trn_dir, "validation"), bs=batch_size if is_mp_mode() else batch_size * get_device_num(), sz=img_size, rect_val=rect_val)
    dshape = [3, img_size, img_size]
    class_dim = 1000

    pyreader = None
    batched_reader = None
    model_name = args.model
    model_list = [m for m in dir(models) if "__" not in m]
    assert model_name in model_list, "{} is not in lists: {}".format(args.model,
                                                                     model_list)
    model = models.__dict__[model_name]()
    with fluid.program_guard(main_prog, startup_prog):
        with fluid.unique_name.guard():
            if is_train:
                with fluid.program_guard(main_prog, py_reader_startup_prog):
                    with fluid.unique_name.guard():
                        pyreader = fluid.layers.py_reader(
                            capacity=batch_size if is_mp_mode() else batch_size * get_device_num(),
                            shapes=([-1] + dshape, (-1, 1)),
                            dtypes=('uint8', 'int64'),
                            name="train_reader_" + str(img_size) if is_train else "test_reader_" + str(img_size),
                            use_double_buffer=True)
                input, label = fluid.layers.read_file(pyreader)
                #pyreader.decorate_paddle_reader(paddle.batch(imagenet_reader.train(os.path.join(args.data_dir, trn_dir, "train")), batch_size=batch_size))
                #pyreader.decorate_paddle_reader(paddle.batch(dataloader.reader(), batch_size=batch_size))
            else:
                input = fluid.layers.data(name="image", shape=[3, 244, 244], dtype="uint8")
                label = fluid.layers.data(name="label", shape=[1], dtype="int64")
                #batched_reader = paddle.batch(dataloader.reader(), batch_size=batch_size) 
            cast_img_type = "float16" if args.fp16 else "float32"
            cast = fluid.layers.cast(input, cast_img_type)
            img_mean = fluid.layers.create_global_var([3, 1, 1], 0.0, cast_img_type, name="img_mean", persistable=True)
            img_std = fluid.layers.create_global_var([3, 1, 1], 0.0, cast_img_type, name="img_std", persistable=True)
            #image = (image - (mean * 255.0)) / (std * 255.0)
            t1 = fluid.layers.elementwise_sub(cast, img_mean, axis=1)
            t2 = fluid.layers.elementwise_div(t1, img_std, axis=1)

            predict = model.net(t2, class_dim=class_dim, img_size=img_size, is_train=is_train)
            cost, pred = fluid.layers.softmax_with_cross_entropy(predict, label, return_softmax=True)
            if args.scale_loss > 1:
                avg_cost = fluid.layers.mean(x=cost) * float(args.scale_loss)
            else:
                avg_cost = fluid.layers.mean(x=cost)

            batch_acc1 = fluid.layers.accuracy(input=pred, label=label, k=1)
            batch_acc5 = fluid.layers.accuracy(input=pred, label=label, k=5)

            # configure optimize
            optimizer = None
            if is_train:
                epochs = [(0,7), (7,13), (13, 22), (22, 25), (25, 28)]
                bs_epoch = [x if is_mp_mode() else x * get_device_num() for x in [224, 224, 96, 96, 50]]
                lrs = [(1.0, 2.0), (2.0, 0.25), (0.42857142857142855, 0.04285714285714286), (0.04285714285714286, 0.004285714285714286), (0.0022321428571428575, 0.00022321428571428573), 0.00022321428571428573]
                images_per_worker = args.total_images / get_device_num() if is_mp_mode() else args.total_images
                optimizer = fluid.optimizer.Momentum(
                    learning_rate=linear_lr_decay_by_epoch(lrs, epochs, bs_epoch, images_per_worker),
                    momentum=0.9,
                    regularization=fluid.regularizer.L2Decay(1e-4))
                if args.fp16:
                    params_grads = optimizer.backward(avg_cost)
                    master_params_grads = utils.create_master_params_grads(
                        params_grads, main_prog, startup_prog, args.scale_loss)
                    optimizer.apply_gradients(master_params_grads)
                    utils.master_param_to_train_param(master_params_grads, params_grads, main_prog)
                else:
                    optimizer.minimize(avg_cost)

    if args.memory_optimize:
        fluid.memory_optimize(main_prog, skip_grads=True)
    if is_train:
        pyreader.decorate_paddle_reader(paddle.batch(dataloader.reader(), batch_size=batch_size, drop_last=True))
    else:
        batched_reader = paddle.batch(dataloader.reader(), batch_size=batch_size if is_mp_mode() else batch_size * get_device_num(), drop_last=True) 

    return avg_cost, optimizer, [batch_acc1,
                                 batch_acc5], batched_reader, pyreader, py_reader_startup_prog, dataloader
def refresh_program(args, epoch, sz, trn_dir, bs, val_bs, need_update_start_prog=False, min_scale=0.08, rect_val=False):
    print('program changed: epoch: [%d], image size: [%d], trn_dir: [%s], batch_size:[%d]' % (epoch, sz, trn_dir, bs))
    train_prog = fluid.Program()
    test_prog = fluid.Program()
    startup_prog = fluid.Program()
    py_reader_startup_prog = fluid.Program()
    num_trainers = args.dist_env["num_trainers"]
    trainer_id = args.dist_env["trainer_id"]

    train_args = build_program(args, True, train_prog, startup_prog, py_reader_startup_prog, sz, trn_dir, bs, min_scale, False)
    test_args = build_program(args, False, test_prog, startup_prog, py_reader_startup_prog, sz, trn_dir, val_bs, min_scale, rect_val)
    gpu_id = int(os.getenv("FLAGS_selected_gpus")) if is_mp_mode() else 0
    place = core.CUDAPlace(gpu_id)
    startup_exe = fluid.Executor(place)
    print("execute py_reader startup program")
    startup_exe.run(py_reader_startup_prog)

    if need_update_start_prog:
        print("execute startup program")
        if is_mp_mode():
            nccl2_prepare(args, startup_prog)
        startup_exe.run(startup_prog)
        conv2d_w_vars = [var for var in startup_prog.global_block().vars.values() if var.name.startswith('conv2d_')]
        for var in conv2d_w_vars:
            torch_w = torch.empty(var.shape)
            #print("initialize %s, shape: %s, with kaiming normalization." % (var.name, var.shape))
            kaiming_np = torch.nn.init.kaiming_normal_(torch_w, mode='fan_out', nonlinearity='relu').numpy()
            tensor = fluid.global_scope().find_var(var.name).get_tensor()
            if args.fp16:
                tensor.set(np.array(kaiming_np, dtype="float16").view(np.uint16), place)
            else:
                tensor.set(np.array(kaiming_np, dtype="float32"), place)

        np_tensors = {}
        np_tensors["img_mean"] = np.array([0.485 * 255.0, 0.456 * 255.0, 0.406 * 255.0]).astype("float16" if args.fp16 else "float32").reshape((3, 1, 1))
        np_tensors["img_std"] = np.array([0.229 * 255.0, 0.224 * 255.0, 0.225 * 255.0]).astype("float16" if args.fp16 else "float32").reshape((3, 1, 1))
        for vname, np_tensor in np_tensors.items():
            var = fluid.global_scope().find_var(vname)
            if args.fp16:
                var.get_tensor().set(np_tensor.view(np.uint16), place)
            else:
                var.get_tensor().set(np_tensor, place)


    strategy = fluid.ExecutionStrategy()
    strategy.num_threads = args.num_threads
    strategy.allow_op_delay = False
    strategy.num_iteration_per_drop_scope = 30
    build_strategy = fluid.BuildStrategy()
    build_strategy.reduce_strategy = fluid.BuildStrategy().ReduceStrategy.AllReduce
    
    
    avg_loss = train_args[0]
    train_exe = fluid.ParallelExecutor(
        True,
        avg_loss.name,
        main_program=train_prog,
        exec_strategy=strategy,
        build_strategy=build_strategy,
        num_trainers=num_trainers,
        trainer_id=trainer_id)

    test_exe = fluid.ParallelExecutor(
        True, main_program=test_prog, share_vars_from=train_exe)

    #return train_args, test_args, test_prog, train_exe, test_exe
    return train_args, test_args, test_prog, train_exe, test_exe

# NOTE: only need to benchmark using parallelexe
def train_parallel(args):
    over_all_start = time.time()
    test_prog = fluid.Program()

    exe = None
    test_exe = None
    train_args = None
    test_args = None
    bs = 224
    val_bs = 64
    for pass_id in range(args.num_epochs):
        # program changed
        if pass_id == 0:
            train_args, test_args, test_prog, exe, test_exe = refresh_program(args, pass_id, sz=128, trn_dir="sz/160/", bs=bs, val_bs=val_bs, need_update_start_prog=True)
        elif pass_id == 13: #13
            bs = 96
            val_bs = 32
            train_args, test_args, test_prog, exe, test_exe = refresh_program(args, pass_id, sz=224, trn_dir="sz/352/", bs=bs, val_bs=val_bs, min_scale=0.087)
        elif pass_id == 25: #25
            bs = 50
            val_bs=4
            train_args, test_args, test_prog, exe, test_exe = refresh_program(args, pass_id, sz=288, trn_dir="", bs=bs, val_bs=val_bs, min_scale=0.5, rect_val=True)
        else:
            pass

        avg_loss = train_args[0]
        num_samples = 0
        iters = 0
        start_time = time.time()
        train_dataloader = train_args[6] # Paddle DataLoader
        train_dataloader.shuffle_seed = pass_id + 1
        train_args[4].start() # start pyreader
        batch_time_start = time.time()
        while True:
            fetch_list = [avg_loss.name]
            acc_name_list = [v.name for v in train_args[2]]
            fetch_list.extend(acc_name_list)
            fetch_list.append("learning_rate")
            if iters > 0 and iters % args.log_period == 0:
                should_print = True
            else:
                should_print = False

            fetch_ret = []
            try:
                if should_print:
                    fetch_ret = exe.run(fetch_list)
                else:
                    exe.run([])
            except fluid.core.EOFException as eof:
                print("Finish current epoch, will reset pyreader...")
                train_args[4].reset()
                break
            except fluid.core.EnforceNotMet as ex:
                traceback.print_exc()
                exit(1)

            num_samples += bs if is_mp_mode() else bs * get_device_num()

            if should_print:
                fetched_data = [np.mean(np.array(d)) for d in fetch_ret]
                print("Pass %d, batch %d, loss %s, accucacys: %s, learning_rate %s, py_reader queue_size: %d, avg batch time: %0.2f " %
                      (pass_id, iters, fetched_data[0], fetched_data[1:-1], fetched_data[-1], train_args[4].queue.size(), (time.time() - batch_time_start) * 1.0 / bs ))
                batch_time_start = time.time()
            iters += 1

        print_train_time(start_time, time.time(), num_samples, "Train")
        feed_list = [test_prog.global_block().var(varname) for varname in ("image", "label")]
        gpu_id = int(os.getenv("FLAGS_selected_gpus")) if is_mp_mode() else 0
        test_feeder = fluid.DataFeeder(feed_list=feed_list, place=fluid.CUDAPlace(gpu_id))
        #test_ret = test_single(test_exe, test_args, args, test_prog, test_feeder, val_bs)
        test_ret = test_parallel(test_exe, test_args, args, test_prog, test_feeder, val_bs)
        print("Pass: %d, Test Accuracy: %s, Spend %.2f hours\n" %
            (pass_id, [np.mean(np.array(v)) for v in test_ret], (time.time() - over_all_start) / 3600))

    print("total train time: ", time.time() - over_all_start)

def print_train_time(start_time, end_time, num_samples, prefix_text=""):
    train_elapsed = end_time - start_time
    examples_per_sec = num_samples / train_elapsed
    print('\n%s Total examples: %d, total time: %.5f, %.5f examples/sed\n' %
          (prefix_text, num_samples, train_elapsed, examples_per_sec))


def print_paddle_envs():
    print('----------- Configuration envs -----------')
    for k in os.environ:
        if "PADDLE_" in k:
            print "ENV %s:%s" % (k, os.environ[k])
    print('------------------------------------------------')


def main():
    args = parse_args()
    args.dist_env = dist_env()
    print_arguments(args)
    print_paddle_envs()
    train_parallel(args)


if __name__ == "__main__":
    main()