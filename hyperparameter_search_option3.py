#!/usr/bin/env python3

import sys,os
import numpy as np
import argparse
import json
import time
import glob
import socket
from mpi4py import MPI

sys.path.append(os.path.dirname(os.path.realpath(__file__))+'/mpi_learn_src')
from mpi_learn.train.algo import Algo
from mpi_learn.train.data import H5Data
from mpi_learn.train.model import ModelFromJsonTF
from mpi_learn.utils import import_keras
import mpi_learn.mpi.manager as mm
from mpi_learn.train.model import ModelFromJsonTF
from mpi_learn.train.GanModel import GANBuilder
from skopt.space import Real, Integer

class BuilderFromFunction(object):
    def __init__(self, model_fn, parameters):
        self.model_fn = model_fn
        self.parameters = parameters

    def builder(self,*params):
        args = dict(zip([p.name for p in self.parameters],params))
        model_json = self.model_fn( **args )
        return ModelFromJsonTF(None,
                               json_str=model_json)

import coordinator
import process_block
import mpiLAPI as mpi

def get_block_num(comm, block_size):
    """
    Gets the correct block number for this process.
    The coordinator (process 0) is in block 999.
    The other processes are divided according to the block size.
    """
    rank = comm.Get_rank()
    if rank == 0:
        return 0
    block_num, rank_in_block = divmod( rank-1, block_size)
    #block_num = int((rank-1) / block_size) + 1
    block_num+=1 ## as blocknum 0 is the skopt-master
    return block_num

def check_sanity(args):
    assert args.block_size > 1, "Block size must be at least 2 (master + worker)"

def make_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verbose', action='store_true')

    parser.add_argument('--batch', help='batch size', default=100, type=int)
    parser.add_argument('--epochs', help='number of training epochs', default=10, type=int)
    parser.add_argument('--optimizer',help='optimizer for master to use',default='adam')
    parser.add_argument('--loss',help='loss function',default='binary_crossentropy')
    parser.add_argument('--early-stopping', type=int, 
            dest='early_stopping', help='patience for early stopping')
    parser.add_argument('--sync-every', help='how often to sync weights with master', 
            default=1, type=int, dest='sync_every')

    parser.add_argument('--block-size', type=int, default=2,
            help='number of MPI processes per block')
    parser.add_argument('--num_iterations', type=int, default=10,
                        help='The number of steps in the skopt process')
    return parser


if __name__ == '__main__':

    print ("I am on",socket.gethostname())
    parser = make_parser()
    args = parser.parse_args()
    check_sanity(args)


    #test = 'topclass'
    test = 'mnist'
    #test = 'gan'
    if test == 'topclass':
        ### topclass example
        model_provider = BuilderFromFunction( model_fn = mpi.test_cnn,
                                              parameters = [ Real(0.0, 1.0, name='dropout'),
                                                             Integer(1,6, name='kernel_size'),
                                                             Real(1.,10., name = 'llr')
                                                         ]
                                          )
        #train_list = glob.glob('/bigdata/shared/LCDJets_Remake/train/04*.h5')
        #val_list = glob.glob('/bigdata/shared/LCDJets_Remake/val/020*.h5')
        train_list = glob.glob('/scratch/snx3000/vlimant/data/LCDJets_Remake/train/*.h5')
        val_list = glob.glob('/scratch/snx3000/vlimant/data/LCDJets_Remake/val/*.h5')
        features_name='Images'
        labels_name='Labels'
    elif test == 'mnist':
        ### mnist example
        model_provider = BuilderFromFunction( model_fn = mpi.test_mnist,
                                              parameters = [ Integer(10,50, name='nb_filters'),
                                                             Integer(2,10, name='pool_size'),
                                                             Integer(2,10, name='kernel_size'),
                                                             Integer(50,200, name='dense'),
                                                             Real(0.0, 1.0, name='dropout')
                                                         ]
        )
        all_list = glob.glob('/scratch/snx3000/vlimant/data/mnist/*.h5')
        l = int( len(all_list)*0.70)
        train_list = all_list[:l]
        val_list = all_list[l:]
        features_name='features'
        labels_name='labels'

    elif test == 'gan':
        ### the gan example
        model_provider = GANBuilder( parameters = [ Integer(50,400, name='latent_size' ),
                                                    Real(0.0, 1.0, name='discr_drop_out')
                                                ]
        )

        all_list = glob.glob('/scratch/snx3000/vlimant/3DGAN/*.h5')
        l = int( len(all_list)*0.70)
        train_list = all_list[:l]
        val_list = all_list[l:]
        features_name='features'
        labels_name='labels'
        
    print (len(train_list),"train files",len(val_list),"validation files")
    print("Initializing...")
    comm_world = MPI.COMM_WORLD.Dup()
    ## consistency check to make sure everything is appropriate
    num_blocks, left_over = divmod( (comm_world.Get_size()-1), args.block_size)
    if left_over:
        print ("The last block is going to be made of {} nodes, make inconsistent block size {}".format( left_over,
                                                                                                         args.block_size))
        num_blocks += 1 ## to accoun for the last block
        if left_over<2:
            print ("The last block is going to be too small for mpi_learn, with no workers")
        sys.exit(1)


    block_num = get_block_num(comm_world, args.block_size)
    device = mm.get_device(comm_world, num_blocks)
    backend = 'tensorflow'
    print("Process {} using device {}".format(comm_world.Get_rank(), device))
    comm_block = comm_world.Split(block_num)
    print ("Process {} sees {} blocks, has block number {}, and rank {} in that block".format(comm_world.Get_rank(),
                                                                                              num_blocks,
                                                                                              block_num,
                                                                                              comm_block.Get_rank()
                                                                                            ))
    ## you need to sync every one up here
    all_block_nums = comm_world.allgather( block_num )
    print ("we gathered all these blocks {}".format( all_block_nums ))
    # MPI process 0 coordinates the Bayesian optimization procedure
    if block_num == 0:
        opt_coordinator = coordinator.Coordinator(comm_world, num_blocks,
                                                  model_provider.parameters)
        opt_coordinator.run(num_iterations=args.num_iterations)
    else:
        print ("Process {} on block {}, rank {}, create a process block".format( comm_world.Get_rank(),
                                                                                 block_num,
                                                                                 comm_block.Get_rank()))
        data = H5Data(batch_size=args.batch, 
                      features_name=features_name,
                      labels_name=labels_name
        )
        data.set_file_names( train_list )
        validate_every = data.count_data()/args.batch 
        print (data.count_data(),"samples to train on")
        algo = Algo(args.optimizer, loss=args.loss, validate_every=validate_every,
                sync_every=args.sync_every) 
        os.environ['KERAS_BACKEND'] = backend
        import_keras()
        import keras.callbacks as cbks
        callbacks = []
        if args.early_stopping is not None:
            callbacks.append( cbks.EarlyStopping( patience=args.early_stopping,
                verbose=1 ) )
        block = process_block.ProcessBlock(comm_world, comm_block, algo, data, device,
                                           model_provider,
                                           args.epochs, train_list, val_list, callbacks, verbose=args.verbose)
        block.run()
