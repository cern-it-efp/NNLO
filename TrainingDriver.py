#!/usr/bin/env python3

### This script creates an MPIManager object and launches distributed training.

import sys,os
import numpy as np
import argparse
import json
import re
import logging

from mpi4py import MPI
from time import time,sleep

from mpi_learn.mpi.manager import MPIManager, get_device
from mpi_learn.train.algo import Algo
from mpi_learn.train.data import H5Data
from mpi_learn.train.model import ModelFromJson, ModelTensorFlow, ModelPytorch
from mpi_learn.utils import import_keras
from mpi_learn.train.trace import Trace
from mpi_learn.logger import initialize_logger

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--verbose',help='display metrics for each training batch',action='store_true')
    parser.add_argument('--profile',help='profile theano code',action='store_true')
    parser.add_argument('--monitor',help='Monitor cpu and gpu utilization', action='store_true')
    parser.add_argument('--trace',help='Record timeline of activity', action='store_true')
    parser.add_argument('--backend', help='specify the backend to be used', choices= ['keras','torch'],default='keras')
    parser.add_argument('--thread_validation', help='run a single process', action='store_true')
    
    # model arguments
    parser.add_argument('--model', help='File containing model architecture (serialized in JSON/pickle, or provided in a .py file')
    parser.add_argument('--trial-name', help='descriptive name for trial', 
            default='train', dest='trial_name')

    # training data arguments
    parser.add_argument('--train_data', help='text file listing data inputs for training', default=None)
    parser.add_argument('--val_data', help='text file listing data inputs for validation', default=None)
    parser.add_argument('--features-name', help='name of HDF5 dataset with input features',
            default='features', dest='features_name')
    parser.add_argument('--labels-name', help='name of HDF5 dataset with output labels',
            default='labels', dest='labels_name')
    parser.add_argument('--batch', help='batch size', default=100, type=int)
    parser.add_argument('--preload-data', help='Preload files as we read them', default=0, type=int, dest='data_preload')
    parser.add_argument('--cache-data', help='Cache the input files to a provided directory', default='', dest='caching_dir')

    # configuration of network topology
    parser.add_argument('--masters', help='number of master processes', default=1, type=int)
    parser.add_argument('--processes', help='number of processes per worker', default=1, type=int)
    parser.add_argument('--max-gpus', dest='max_gpus', help='max GPUs to use', 
            type=int, default=-1)
    parser.add_argument('--master-gpu',help='master process should get a gpu',
            action='store_true', dest='master_gpu')
    parser.add_argument('--synchronous',help='run in synchronous mode',action='store_true')

    # configuration of training process
    parser.add_argument('--epochs', help='number of training epochs', default=1, type=int)
    parser.add_argument('--optimizer',help='optimizer for master to use',default='adam')
    parser.add_argument('--loss',help='loss function',default='binary_crossentropy')
    parser.add_argument('--early-stopping', default=None,
            dest='early_stopping', help='patience for early stopping')
    parser.add_argument('--target-metric', default=None,
                        dest='target_metric', help='Passing configuration for a target metric')
    parser.add_argument('--worker-optimizer',help='optimizer for workers to use',
            dest='worker_optimizer', default='sgd')
    parser.add_argument('--worker-optimizer-params',help='worker optimizer parameters (string representation of a dict)',
            dest='worker_optimizer_params', default='{}')
    parser.add_argument('--sync-every', help='how often to sync weights with master', 
            default=1, type=int, dest='sync_every')
    parser.add_argument('--mode',help='Mode of operation.'
                        'One of "sgd" (Stohastic Gradient Descent), "easgd" (Elastic Averaging SGD) or "gem" (Gradient Energy Matching)',default='sgd',choices=['sgd','easgd','gem'])
    parser.add_argument('--elastic-force',help='beta parameter for EASGD',type=float,default=0.9)
    parser.add_argument('--elastic-lr',help='worker SGD learning rate for EASGD',
            type=float, default=1.0, dest='elastic_lr')
    parser.add_argument('--elastic-momentum',help='worker SGD momentum for EASGD',
            type=float, default=0, dest='elastic_momentum')
    parser.add_argument('--gem-lr',help='learning rate for GEM',type=float,default=0.01, dest='gem_lr')
    parser.add_argument('--gem-momentum',help='momentum for GEM',type=float, default=0.9, dest='gem_momentum')
    parser.add_argument('--gem-kappa',help='Proxy amplification parameter for GEM',type=float, default=2.0, dest='gem_kappa')
    parser.add_argument('--restore', help='pass a file to retore the variables from', default=None)
    parser.add_argument('--checkpoint', help='Base name of the checkpointing file. If omitted no checkpointing will be done', default=None)
    parser.add_argument('--checkpoint-interval', help='Number of epochs between checkpoints', default=5, type=int, dest='checkpoint_interval')

    # logging configuration
    parser.add_argument('--log-file', default=None, dest='log_file', help='log file to write, in additon to output stream')
    parser.add_argument('--log-level', default='info', dest='log_level', help='log level (debug, info, warn, error)')

    args = parser.parse_args()

    initialize_logger(filename=args.log_file, file_level=args.log_level, stream_level=args.log_level)

    a_backend = args.backend
    if 'torch' in args.model:
        a_backend = 'torch'
        
    m_module = __import__(args.model.replace('.py','')) if '.py' in args.model else None
        
    if args.train_data:
        with open(args.train_data) as train_list_file:
            train_list = [ s.strip() for s in train_list_file.readlines() ]
    elif m_module is not None:
        train_list = m_module.get_train()
    else:
        logging.info("no training data provided")
        
    if args.val_data:
        with open(args.val_data) as val_list_file:
            val_list = [ s.strip() for s in val_list_file.readlines() ]
    elif m_module is not None:
        val_list = m_module.get_val()
    else:
        logging.info("no validation data provided")
        
    comm = MPI.COMM_WORLD.Dup()

    if args.trace: Trace.enable()

    model_weights = None
    use_tf = a_backend == 'keras'
    use_torch = not use_tf
    
    if args.restore:
        args.restore = re.sub(r'\.algo$', '', args.restore)
        if os.path.isfile(args.restore + '.latest'):
            with open(args.restore + '.latest', 'r') as latest:
                args.restore = latest.read().splitlines()[-1]
        if use_torch and os.path.isfile(args.restore + '.model'):
            model_weights = args.restore + '.model'
        if use_torch:
            model_weights += '_w'

    # Theano is the default backend; use tensorflow if --tf is specified.
    # In the theano case it is necessary to specify the device before importing.
    device = get_device( comm, args.masters, gpu_limit=args.max_gpus,
                gpu_for_master=args.master_gpu)
    hide_device = True
    if use_torch:
        logging.debug("Using pytorch")
        if not args.optimizer.endswith("torch"):
            args.optimizer = args.optimizer + 'torch'
        import torch
        if hide_device:
            os.environ['CUDA_VISIBLE_DEVICES'] = device[-1] if 'gpu' in device else ''
            logging.debug('set to device %s',os.environ['CUDA_VISIBLE_DEVICES'])
        else:
            if 'gpu' in device:
                torch.cuda.set_device(int(device[-1]))
        if m_module and hasttar("builder", m_module):
            model_builder = m_module.builder
        else:
            model_builder = ModelPytorch(comm, source=args.model, weights=model_weights, gpus=1 if 'gpu' in device else 0)
    else:
        logging.debug("Using TensorFlow")
        if not args.optimizer.endswith("tf"):
            args.optimizer = args.optimizer + 'tf'
        if hide_device:
            os.environ['CUDA_VISIBLE_DEVICES'] = device[-1] if 'gpu' in device else ''
            logging.debug('set to device %s',os.environ['CUDA_VISIBLE_DEVICES'])
        os.environ['KERAS_BACKEND'] = 'tensorflow'

        import_keras()
        import keras.backend as K
        gpu_options=K.tf.GPUOptions(
            per_process_gpu_memory_fraction=0.1, #was 0.0
            allow_growth = True,
            visible_device_list = device[-1] if 'gpu' in device else '')
        if hide_device:
            gpu_options=K.tf.GPUOptions(
                per_process_gpu_memory_fraction=0.0,
                allow_growth = True,)        
        K.set_session( K.tf.Session( config=K.tf.ConfigProto(
            allow_soft_placement=True, log_device_placement=False,
            gpu_options=gpu_options
        ) ) )
        tf_device = device
        if hide_device:
            tf_device = 'gpu0' if 'gpu' in device else ''
        if m_module and hasttar("builder", m_module):
            model_builder = m_module.builder( comm, source=args.model, device_name=tf_device , weights=model_weights)
        else:
            model_builder = ModelTensorFlow( comm, source=args.model, device_name=tf_device , weights=model_weights)
        logging.debug("Using device {}".format(model_builder.device))

        if args.profile:
            os.environ['CUDA_LAUNCH_BLOCKING'] = '1'


    data = H5Data( batch_size=args.batch,
                   cache = args.caching_dir,
                   preloading = args.data_preload,
                   features_name=args.features_name, labels_name=args.labels_name )
    # We initialize the Data object with the training data list
    # so that we can use it to count the number of training examples
    data.set_file_names( train_list )
    validate_every = int(data.count_data()/args.batch)

    # Some input arguments may be ignored depending on chosen algorithm
    if args.mode == 'easgd':
        algo = Algo(None, loss=args.loss, validate_every=validate_every,
                mode='easgd', sync_every=args.sync_every,
                worker_optimizer=args.worker_optimizer,
                worker_optimizer_params=args.worker_optimizer_params,
                elastic_force=args.elastic_force/(comm.Get_size()-1),
                elastic_lr=args.elastic_lr, 
                elastic_momentum=args.elastic_momentum) 
    elif args.mode == 'gem':
        algo = Algo('gem', loss=args.loss, validate_every=validate_every,
                mode='gem', sync_every=args.sync_every,
                worker_optimizer=args.worker_optimizer,
                worker_optimizer_params=args.worker_optimizer_params,
                learning_rate=args.gem_lr, momentum=args.gem_momentum, kappa=args.gem_kappa)
    elif args.mode == 'sgd':
        algo = Algo(args.optimizer, loss=args.loss, validate_every=validate_every,
                sync_every=args.sync_every, worker_optimizer=args.worker_optimizer,
                worker_optimizer_params=args.worker_optimizer_params)
    else:
        logging.info("%s not supported mode", args.mode)
        
    if args.restore:
        algo.load(args.restore)

    # Creating the MPIManager object causes all needed worker and master nodes to be created
    manager = MPIManager( comm=comm, data=data, algo=algo, model_builder=model_builder,
                          num_epochs=args.epochs, train_list=train_list, val_list=val_list, 
                          num_masters=args.masters, num_processes=args.processes,
                          synchronous=args.synchronous, 
                          verbose=args.verbose, monitor=args.monitor,
                          early_stopping=args.early_stopping,
                          target_metric=args.target_metric,
                          thread_validation = args.thread_validation,
                          checkpoint=args.checkpoint, checkpoint_interval=args.checkpoint_interval)


    # Process 0 launches the training procedure
    if comm.Get_rank() == 0:
        logging.debug('Training configuration: %s', algo.get_config())

        t_0 = time()
        histories = manager.process.train() 
        delta_t = time() - t_0
        manager.free_comms()
        logging.info("Training finished in {0:.3f} seconds".format(delta_t))

        if args.model.endswith('.py'):
            module = __import__(args.model.replace('.py',''))
            try:
                model_name = module.get_name()
            except:
                model_name = os.path.basename(args.model).replace('.py','')
        else:
            model_name = os.path.basename(args.model).replace('.json','')

        json_name = '_'.join([model_name,args.trial_name,"history.json"])
        manager.process.record_details(json_name,
                                       meta={"args":vars(args)})            
        logging.info("Wrote trial information to {0}".format(json_name))

    comm.barrier()
    logging.info("Terminating")
    if args.trace: Trace.collect(clean=True)
