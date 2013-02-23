'''
Created on Feb 20, 2011

@author: mkiyer

AssemblyLine: transcriptome meta-assembly from RNA-Seq

Copyright (C) 2012 Matthew Iyer

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''
import logging
import collections
import networkx as nx

from assemblyline.lib.transcript import Exon, NEG_STRAND
from base import NODE_SCORE, CHAIN_NODES, \
    SMOOTH_FWD, SMOOTH_REV, SMOOTH_TMP, PathInfo
from path_finder import find_suboptimal_paths
from smooth import smooth_graph

SOURCE = -1
SINK = -2

def get_start_end_nodes(G):
    # get all leaf nodes
    start_nodes = set()
    end_nodes = set()
    for n in G.nodes_iter():
        if G.in_degree(n) == 0:
            start_nodes.add(n)
        if G.out_degree(n) == 0:
            end_nodes.add(n)
    return start_nodes, end_nodes

def init_node_attrs():
    return {NODE_SCORE: 0.0, SMOOTH_FWD: 0.0, 
            SMOOTH_REV: 0.0, SMOOTH_TMP: 0.0}

def add_path(K, path, score):
    # add first kmer
    from_id = path[0]
    if from_id not in K:
        K.add_node(from_id, attr_dict=init_node_attrs())
    kmerattrs = K.node[from_id]
    kmerattrs[NODE_SCORE] += score
    # the first kmer should be "smoothed" in reverse direction
    kmerattrs[SMOOTH_REV] += score
    for to_id in path[1:]:
        if to_id not in K:
            K.add_node(to_id, attr_dict=init_node_attrs())
        kmerattrs = K.node[to_id]
        kmerattrs[NODE_SCORE] += score
        # connect kmers
        K.add_edge(from_id, to_id)
        # update from_kmer to continue loop
        from_id = to_id
    # the last kmer should be "smoothed" in forward direction
    kmerattrs[SMOOTH_FWD] += score

def hash_kmers(id_kmer_map, k, ksmall):
    kmer_hash = collections.defaultdict(lambda: set())
    for kmer_id,kmer in id_kmer_map.iteritems():
        for i in xrange(k - (ksmall-1)):
            kmer_hash[kmer[i:i+ksmall]].add(kmer_id)
    return kmer_hash

def extrapolate_short_path(kmer_hash, kmer_score_dict, path, score):
    """
    add the list of 'partial_paths' paths to graph 'K'

    paths shorter than 'k' are extrapolated into k-mers before being added 
    to the graph
    """
    if path not in kmer_hash:
        return []
    total_score = 0
    matching_kmers = []
    for kmer in kmer_hash[path]:
        # get approximate score at kmer
        kmer_score = kmer_score_dict[kmer]        
        # compute total score at matching kmers
        total_score += kmer_score
        matching_kmers.append((kmer, kmer_score))
    # now calculate fractional densities for matching kmers
    new_partial_paths = [] 
    for kmer,kmer_score in matching_kmers:
        new_score = score * (kmer_score / total_score)
        new_partial_paths.append((kmer, new_score))
    return new_partial_paths

def find_short_path_kmers(kmer_hash, K, path, score):
    """
    find kmers where 'path' is a subset and partition 'score'
    of path proportionally among all matching kmers

    generator function yields (kmer_id, score) tuples
    """
    if path not in kmer_hash:
        return
    matching_kmers = []
    total_score = 0.0
    for kmer_id in kmer_hash[path]:
        # compute total score at matching kmers
        kmer_score = K.node[kmer_id][NODE_SCORE]      
        total_score += kmer_score
        matching_kmers.append((kmer_id, kmer_score))
    # now calculate fractional densities for matching kmers
    for kmer_id,kmer_score in matching_kmers:
        new_score = score * (kmer_score / float(total_score))
        yield ([kmer_id], new_score)

def clip_dangling_ends(K):
    """
    fragmented transcripts can manifest as 0-degree
    dangling ends of the overlap graph. clip these 
    ends so that every node can be included in a path
    from 'source' to 'sink'
    """
    # TODO: optimize this function using adjacency_iter graph traversal
    source = K.graph['source']
    sink = K.graph['sink']
    clip_nodes = set()
    clip_score = 0.0
    test_nodes = set(K.nodes())
    while len(test_nodes) > 0:
        new_test_nodes = set()
        new_clip_nodes = set()
        for n in test_nodes:
            if (n == source) or (n == sink):
                continue
            ind = K.in_degree(n)
            outd = K.out_degree(n)
            if (ind == 0) or (outd == 0):
                new_clip_nodes.add(n)
                # check the neighbors of this node after it is removed
                if ind == 0:
                    new_test_nodes.update(K.successors(n))
                else:
                    new_test_nodes.update(K.predecessors(n))
        # update
        test_nodes = new_test_nodes.difference(new_clip_nodes)
        clip_nodes.update(new_clip_nodes)
        clip_score += sum(K.node[n][NODE_SCORE] for n in new_clip_nodes)
        # remove nodes
        K.remove_nodes_from(new_clip_nodes)
    return clip_nodes, clip_score

def connect_dangling_ends(K):
    """
    fragmented transcripts can manifest as 0-degree dangling ends
    of the overlap graph. unlike 'clip_dangling_ends' this function
    connects these ends to the 'source' and/or 'sink' nodes
    """
    source = K.graph['source']
    sink = K.graph['sink']
    # connect all nodes with degree zero to the source/sink nodes
    # to account for fragmentation in the kmer graph when k > 2
    edges_to_add = []
    for n in K.nodes_iter():
        if (n == source) or (n == sink):
            continue
        if K.in_degree(n) == 0:
            edges_to_add.append((source, n))
        if K.out_degree(n) == 0:
            edges_to_add.append((n,sink))
    # connect kmers
    for u,v in edges_to_add:
        K.add_edge(u,v)

def create_kmer_graph(G, partial_paths, k):
    """
    create kmer graph from partial paths

    partial_paths is a list of (path,score) tuples
    """
    def get_kmers(path, k):
        for i in xrange(0, len(path) - (k-1)):
            yield path[i:i+k]
    # find all beginning/end nodes in linear graph
    start_nodes, end_nodes = get_start_end_nodes(G)
    # convert paths to k-mers and create a k-mer to 
    # integer node map
    kmer_id_map = {}
    id_kmer_map = {}
    kmer_paths = []
    current_id = 0
    short_partial_path_dict = collections.defaultdict(lambda: [])
    for path, score in partial_paths:
        # check for start and end nodes
        is_start = (path[0] in start_nodes)
        is_end = (path[-1] in end_nodes)
        full_length = is_start and is_end
        if (len(path) < k) and (not full_length):
            # save fragmented short paths
            short_partial_path_dict[len(path)].append((path,score))
            continue
        # convert to path of kmers
        kmerpath = []
        if is_start:
            kmerpath.append(SOURCE)
        if len(path) < k:        
            # specially add short full length paths because
            # they are not long enough to have kmers
            kmers = [path]
        else:
            kmers = get_kmers(path, k)
        # convert to path of kmers
        for kmer in kmers:
            if kmer not in kmer_id_map:
                kmer_id = current_id
                kmer_id_map[kmer] = current_id
                id_kmer_map[current_id] = kmer
                current_id += 1
            else:
                kmer_id = kmer_id_map[kmer]
            kmerpath.append(kmer_id)
        if is_end:
            kmerpath.append(SINK)

        kmer_paths.append((kmerpath, score))
    # create a graph of k-mers
    K = nx.DiGraph()
    K.graph['id_kmer_map'] = id_kmer_map
    K.graph['source'] = SOURCE
    K.graph['sink'] = SINK
    for path, score in kmer_paths:
        add_path(K, path, score)
    # try to add short paths to graph if they are exact subpaths of 
    # existing kmers
    kmer_paths = []
    lost_paths = []
    for ksmall, short_partial_paths in short_partial_path_dict.iteritems():
        kmer_hash = hash_kmers(id_kmer_map, k, ksmall)
        for path, score in short_partial_paths:
            matching_paths = list(find_short_path_kmers(kmer_hash, K, path, score))
            if len(matching_paths) == 0:
                lost_paths.append((path,score))
            kmer_paths.extend(matching_paths)
    # add new paths
    for path, score in kmer_paths:
        add_path(K, path, score)
    # connect all kmer nodes with degree zero to the source/sink node
    # to account for fragmentation in the kmer graph when k > 2
    clip_nodes, clip_score = clip_dangling_ends(K)
    # cleanup
    del kmer_id_map
    return K, lost_paths, clip_nodes, clip_score

def optimize_k(G, partial_paths, kmin, kmax, sensitivity_threshold):
    """
    determine optimal choice for parameter 'k' for assembly
    maximizes k while ensuring sensitivity constraint is met
    """
    total_score = sum(len(path)*score for path,score in partial_paths)
    last = None
    for k in xrange(kmin, kmax+1):
        # create k-mer graph and add partial paths
        K, lost_paths, clip_nodes, clip_score = \
            create_kmer_graph(G, partial_paths, k)
        lost_path_score = sum(len(path)*score for path,score in lost_paths)
        total_lost_score = lost_path_score + clip_score
        kept_score = total_score - total_lost_score
        score_sensitivity = kept_score / total_score
        logging.debug("\t\toptimize k=%d n=%d e=%d p=%d "
                      "clipped=%d(%.1f%%) score=%.3f(%.1f%%) "
                      "lost_paths=%d(%.1f%%) score=%.3f(%.1f%%) "
                      "total=%.1f/%.3f(%.1f%%) " 
                      "sens=%.3f" %
                      (k, len(K), K.number_of_edges(), len(partial_paths),
                       len(clip_nodes), 100*float(len(clip_nodes))/len(K),
                       clip_score, 100*clip_score/total_score,
                       len(lost_paths), 100*float(len(lost_paths))/len(partial_paths),
                       lost_path_score, 100*lost_path_score/total_score,
                       total_lost_score, total_score,
                       100*total_lost_score/total_score,
                       score_sensitivity))
        if score_sensitivity < sensitivity_threshold:
            break
        last = (K, k)
    return last


def expand_path_chains(G, strand, path):
    # reverse negative stranded data so that all paths go from 
    # small -> large genomic coords
    if strand == NEG_STRAND:
        path.reverse()
    # get chains (children nodes) along path
    newpath = []
    for n in path:
        nd = G.node[n]
        if CHAIN_NODES in nd:
            newpath.extend(nd[CHAIN_NODES])
        else:
            newpath.append(n)
    path = newpath
    # collapse contiguous nodes along path
    newpath = []
    chain = [path[0]]
    for v in path[1:]:
        if chain[-1].end != v.start:
            # update path with merge chain node
            newpath.append(Exon(chain[0].start, 
                                chain[-1].end))
            # reset chain
            chain = []
        chain.append(v)
    # add last chain
    newpath.append(Exon(chain[0].start, chain[-1].end))
    return newpath

def assemble_transcript_graph(G, strand, partial_paths, 
                              user_kmax, ksensitivity,
                              fraction_major_path, 
                              max_paths):
    """
    enumerates individual transcript isoforms from transcript graph using
    a greedy algorithm
    
    strand: strand of graph G
    fraction_major_path: only return isoforms with score greater than 
    some fraction of the highest score path
    max_paths: do not enumerate more than max_paths isoforms     
    """
    # constrain sensitivity parameter
    ksensitivity = min(max(0.0, ksensitivity), 1.0)
    # constrain fraction_major_path parameter
    fraction_major_path = min(max(0.0, fraction_major_path), 1.0)
    # determine parameter 'k' for de bruijn graph assembly
    if user_kmax > 0:
        kmax = user_kmax
    else: 
        kmax = max(len(x[0]) for x in partial_paths)
    # allow optimization of 'k' if ksensitivity parameter is set
    if ksensitivity > 0:
        kmin = 1
    else:
        kmin = kmax
    K, k = optimize_k(G, partial_paths, kmin, kmax, ksensitivity)
    logging.debug("\tConstructing k-mer graph with k=%d" % (k))
    # smooth kmer graph
    smooth_graph(K)
    # find up to 'max_paths' paths through graph
    logging.debug("\tFinding suboptimal paths in k-mer graph with %d nodes" % (len(K)))
    path_info_list = []
    id_kmer_map = K.graph['id_kmer_map']   
    for kmer_path, score in find_suboptimal_paths(K, K.graph['source'], 
                                                  K.graph['sink'],
                                                  fraction_major_path, 
                                                  max_paths):
        # reconstruct path from kmer ids
        path = list(id_kmer_map[kmer_path[1]])
        path.extend(id_kmer_map[n][-1] for n in kmer_path[2:-1])
        # cleanup and expand nodes within path
        path = expand_path_chains(G, strand, path)
        # add to path list
        path_info_list.append(PathInfo(score, path))
        logging.debug("\t\tscore=%f num_exons=%d" % (score, len(path)))
    return path_info_list
