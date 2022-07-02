import numpy as np
import dgl
import torch
import argparse
from dgl.data import register_data_args
from dgl.data import CoraGraphDataset
from dgl.data import CiteseerGraphDataset
from dgl.data import PubmedGraphDataset
from dgl.data import RedditDataset
from ogb.nodeproppred import DglNodePropPredDataset
from modules import *
import sys

full = np.load('arxiv2_full.npy', allow_pickle=True)
trunc = np.load('arxiv2_trunc.npy', allow_pickle=True)

num_nodes = full.shape[0]
len_features = 768

num_PEs = 64
num_chunks = 256
num_cache = 256
fifo_size = 5
num_heads = 8
DSP_Dense = 4096
DSP_VP = 32
DU_delay = int(np.log2(num_chunks)) - 2
VP_delay = int(np.log2(num_PEs))-2
VP_time = int(len_features/DSP_VP) * 2
CB_delay = int(np.log2(num_PEs))-2
CB_time = int(len_features/DSP_VP)
#cache_size = 20 * 1024 * 1024 / 4 / num_cache / len_features

def resources():
    num_DSP = 6840
    num_SRAM = 35 * 1024 * 1024
    used_SRAM = num_nodes * 2 * num_heads * 4
    used_DSP = num_PEs  # exponent
    used_DSP += num_PEs * DSP_VP

    print(len_features)
    return

DU = [Decoder_Unit(i, DU_delay) for i in range(num_PEs)]
SSR = [Swap_Shift_Register(i) for i in range(num_PEs)]
VP = [Vector_Processor(i, num_cache, VP_delay, VP_time) for i in range(num_PEs)]
fifo = [e_FIFO(i, fifo_size) for i in range(num_chunks)]
CB = [CacheBlock(i, 27, 5, CB_delay, CB_time) for i in range(num_cache)]
DDR = [DDRModel(i, 5, int(len_features/DSP_VP), 72) for i in range(4)]

time = 0   #全局time
time_next = 10000
node_i = 0

print(num_nodes)

def top():
    if (node_i < num_nodes):
        return True
    else:
        for i in range(num_PEs):
            if (DU[i].idle == False): return True
        for i in range(num_chunks):
            if (not fifo[i].empty()): return True
        for i in range(num_PEs):
            if (SSR[i].idle == False): return True
        for i in range(num_PEs):
            if (VP[i].idle == False): return True
        for i in range(num_cache):
            if (not CB[i].fifo.empty()): return True
        for i in range(4):
            if (not DDR[i].waitlist.empty()): return True
            if (not DDR[i].timelist == []): return True

        return False


cache_block_size = 4
cache_size = 256
hmf = [0, 0, 0]
total = [0]


while (top()):
    for i in range(num_PEs):
        if (DU[i].time_stamp > time):
            if (DU[i].time_stamp < time_next): time_next = DU[i].time_stamp
        elif (DU[i].idle and SSR[i].idle and node_i < num_nodes):
            print("register DU", i, "for node", node_i, "at time:", time)
            nlist= [node_i] + full[node_i]
            DU[i].register(node_i, nlist, time)
            SSR[i].register(node_i, len(nlist))
            node_i = node_i + 1
            if (DU[i].time_stamp < time_next): time_next = DU[i].time_stamp
        elif (DU[i].idle == False):
            if (DU[i].time_stamp < time): DU[i].time_stamp = time
            DU[i].step(fifo)
            if (DU[i].time_stamp < time_next): time_next = DU[i].time_stamp
    #print(time,time_next)

    for i in range(num_chunks):
        if (fifo[i].time_stamp > time):
            if (fifo[i].time_stamp < time_next): time_next = fifo[i].time_stamp
        elif (not fifo[i].empty()):
            if (fifo[i].time_stamp < time): fifo[i].time_stamp = time
            fifo[i].step(SSR)
            #print("fifo", i ,"step")
            if (fifo[i].time_stamp < time_next): time_next = fifo[i].time_stamp
    #print(time,time_next)

    for i in range(num_PEs):
        if (SSR[i].time_stamp > time):
            if (SSR[i].time_stamp < time_next): time_next = SSR[i].time_stamp
        elif (SSR[i].idle == False and SSR[i].count == SSR[i].degree and VP[i].idle): #SSR未满或者VP在工作,什么都不做
            if (SSR[i].time_stamp < time): SSR[i].time_stamp = time
            SSR[i].step(VP, trunc, total)
            #print("SSR", i ,"step")
            if (SSR[i].time_stamp < time_next): time_next = SSR[i].time_stamp
    #print(time, time_next)

    for i in range(num_PEs):
        if (VP[i].time_stamp > time):
            if (VP[i].time_stamp < time_next): time_next = VP[i].time_stamp
        elif (VP[i].idle == False):
            if (VP[i].time_stamp < time): VP[i].time_stamp = time
            VP[i].step(CB, hmf)
            if (VP[i].time_stamp < time_next): time_next = VP[i].time_stamp
    #print(time, time_next)

    for i in range(num_cache):
        if (CB[i].time_stamp > time):
            if (CB[i].time_stamp < time_next): time_next = CB[i].time_stamp
        elif (not CB[i].fifo.empty() or CB[i].fetch_start or CB[i].AXI_return):
            if (CB[i].time_stamp < time): CB[i].time_stamp = time
            CB[i].step(VP, DDR, hmf)
            #print("CB", i ,"step")
            if (CB[i].time_stamp < time_next): time_next = CB[i].time_stamp
    #print(time, time_next)

    for i in range(4):
        if (DDR[i].time_stamp > time):
            if (DDR[i].time_stamp < time_next): time_next = DDR[i].time_stamp
        elif (not DDR[i].waitlist.empty() or DDR[i].timelist != []):
            if (DDR[i].time_stamp < time): DDR[i].time_stamp = time
            DDR[i].step(CB)
            #print("DDR", i ,"step")
            if (DDR[i].time_stamp < time_next): time_next = DDR[i].time_stamp
    #print(time, time_next)

    if (time_next == time):
        print("Error at time:", time, "time_next equals time!")
        break
    if (time_next == time + 10000):
        for i in range(num_PEs):
            print(SSR[i].idle, SSR[i].count, SSR[i].degree)
        for i in range(num_PEs):
            print(VP[i].idle, VP[i].finished, VP[i].depth)
        print("Error at time:", time, "no step happened")
        break
    time = time_next
    time_next = time + 10000
print(time)
print(total[0])
print(hmf[0],hmf[1], hmf[2])