# coding=utf-8
import os
import shutil

import numpy as np
import tensorflow as tf
from scipy.sparse import coo_matrix


#######################################################################################################################
## GRAPH OBJECT CLASS #################################################################################################
#######################################################################################################################
class GraphObject:
    """ Homogeneous Graph data representation. Non-Composite GNNs are based on this class. """

    ## CONSTRUCTORS METHODs ###########################################################################################
    def __init__(self, nodes, arcs, targets,
                 focus: str = 'n',
                 set_mask=None,
                 output_mask=None,
                 sample_weight=1,
                 NodeGraph=None,
                 aggregation_mode: str = 'sum',
                 dtype='float32'):
        """ CONSTRUCTOR METHOD

        :param nodes: Ordered Nodes Matrix X where nodes[i, :] = [i-th node Label].
        :param arcs: Ordered Arcs Matrix E where arcs[i, :] = [From ID Node | To ID Node | i-th arc Label].
                     Note that [From ID Node | To ID Node] are used only for building Andjacency Matrix.
                     Edge matrices is composed of only edge features.
        :param targets: Targets Matrix T with shape (Num of arcs/node targeted example or 1, dim_target example).
        :param focus: (str) The problem on which graph is used: 'a' arcs-focused, 'g' graph-focused, 'n' node-focused.
        :param set_mask: Array of boolean {0,1} to define arcs/nodes belonging to a set, when dataset == single GraphObject.
        :param output_mask: Array of boolean {0,1} to define the sub-set of arcs/nodes whose target is known.
        :param sample_weight: target sample weight for loss computation. It can be int, float or numpy.array of ints or floats:
            > If int or float, all targets are weighted as sample_weight * ones.
            > If numpy.array, len(sample_weight) and targets.shape[0] must agree.
        :param NodeGraph: Sparse matrix in coo format of shape (nodes.shape[0], {Num graphs or 1}) used only when focus=='g'.
        :param aggregation_mode: (str) The aggregation mode for the incoming message based on ArcNode and Adjacency matrices:
            ---> elem(matrix)={0-1};
            > 'average': A'X gives the average of incoming messages, s.t. sum(A[:,i])==1;
            > 'normalized': A'X gives the normalized message wrt the total number of g.nodes, s.t. sum(A)==1;
            > 'sum': A'X gives the total sum of incoming messages, s.t. A={0,1}.
        :param dtype: set dtype for dense and sparse matrices. Default to np.float32. """
        self.dtype = np.dtype(dtype)

        # store arcs, nodes, targets and sample weight.
        arcs = np.unique(arcs, axis=0)
        self.arcs = arcs[:, 2:].astype(self.dtype)
        self.nodes = nodes.astype(self.dtype)
        self.targets = targets.astype(self.dtype)
        self.sample_weight = (sample_weight * np.ones(self.targets.shape[0])).astype(self.dtype)

        # store dimensions: note that first two columns of arcs contain nodes indices.
        self.DIM_NODE_LABEL = np.array(nodes.shape[1], ndmin=1, dtype=int)
        self.DIM_ARC_LABEL = self.arcs.shape[1]
        self.DIM_TARGET = self.targets.shape[1]

        # setting the problem the graph is focused on: node, arcs or graph focused.
        lenMask = {'n': nodes.shape[0], 'a': arcs.shape[0], 'g': nodes.shape[0]}

        # build set_mask and output mask
        # for a dataset composed of only a single graph, its nodes must be divided into training, test and validation set.
        self.set_mask = np.ones(lenMask[focus], dtype=bool) if set_mask is None else set_mask.astype(bool)
        self.output_mask = np.ones(lenMask[focus], dtype=bool) if output_mask is None else output_mask.astype(bool)

        # check lengths: output_mask must be as long as set_mask, if passed as parameter to constructor.
        if len(self.set_mask) != len(self.output_mask): raise ValueError('Error - len(<set_mask>) != len(<output_mask>)')

        # set nodes and arcs aggregation.
        self.checkAggregation(aggregation_mode)
        self.aggregation_mode = str(aggregation_mode)

        # build Adjancency Matrix A.
        # Note that it may be an aggregated version of the 'normal' Adjacency Matrix (with only 0 and 1),
        # since each element is set from aggregation mode.
        self.Adjacency = self.buildAdjacency(arcs[:, :2])

        # build ArcNode matrix or set from constructor's parameter.
        self.ArcNode = self.buildArcNode()

        # build node_graph conversion matrix, to transform a node-focused output into a graph-focused one.
        self.NodeGraph = self.buildNodeGraph(focus) if NodeGraph is None else coo_matrix(NodeGraph, dtype=self.dtype)

    # -----------------------------------------------------------------------------------------------------------------
    def buildAdjacency(self, indices):
        """ Build the 'Aggregated' Adjacency Matrix ADJ, s.t. ADJ[i,j]=value if edge (i,j) exists in E.

        Note that if the edge is bidirection, both (i,j) and (j,i) must exist in indices.
        Values are set by self.aggregation_mode: 'sum':1, 'normalized':1/self.nodes.shape[0], 'average':1/number_of_neighbors.

        :return: sparse ArcNode Matrix in coo format, for memory efficiency. """

        # column indices of A are located in the second column of the arcs tensor,
        # since they represent the node id pointed by the corresponding arc.
        # row == arc id, is an ordered array of int from 0 to number of arcs.
        indices = indices.astype(int)
        col = indices[:, 1]

        # SUM node aggregation
        # incoming message as sum of neighbors states and labels.
        # ADJ in {0, 1}.
        values = np.ones(len(col))

        # NORMALIZED node aggregation
        # incoming message as sum of neighbors states and labels divided by the number of nodes in the graph.
        # sum(ADJ)==1.
        if self.aggregation_mode == 'normalized':
            values = values * float(1 / len(col))

        # AVERAGE node aggregation
        # incoming message as average of neighbors states and labels.
        # sum(ADJ[:, i])==1.
        elif self.aggregation_mode == 'average':
            val, col_index, destination_node_counts = np.unique(col, return_inverse=True, return_counts=True)
            values = values / destination_node_counts[col_index]

        # isolated nodes correction: if nodes[i] is isolated, then ADJ[:,i]=0, to maintain nodes ordering.
        return coo_matrix((values, (indices[:, 0], col)), shape=(self.nodes.shape[0], self.nodes.shape[0]), dtype=self.dtype)

    # -----------------------------------------------------------------------------------------------------------------
    def buildArcNode(self):
        """ Build ArcNode Matrix AN of shape (number_of_arcs, number_of_nodes) where A[i,j]=value if arc[i, 1]==j-th node.
        Compute the matmul(A, msg:=message) to get the incoming message on each node.

        :return: sparse ArcNode Matrix in coo format, for memory efficiency. """
        values = self.Adjacency.data
        indices = (np.arange(0, len(values)), self.Adjacency.col)

        # isolated nodes correction: if nodes[i] is isolated, then AN[:,i]=0, to maintain nodes ordering.
        return coo_matrix((values, indices), shape=(self.arcs.shape[0], self.nodes.shape[0]), dtype=self.dtype)

    # -----------------------------------------------------------------------------------------------------------------
    def buildNodeGraph(self, focus: str):
        """ Build Node-Graph Aggregation Matrix, to transform a node-focused gnn output in a graph-focused one.

        NodeGraph != empty only if focus == 'g': It has dimensions (nodes.shape[0], 1) for a single graph,
        or (nodes.shape[0], Num graphs) for a graph containing 2+ graphs, built by merging the single graphs into a bigger one,
        such that after the node-graph aggregation process gnn can compute (Num graphs, targets.shape[1]) as output.

        :return: non-empty NodeGraph sparse matrix in coo_format:
        if :param focus: is 'g', as NodeGraph is used in graph-focused problems. """
        if focus == 'g': data = np.ones((self.nodes.shape[0], 1)) * (1 / self.nodes.shape[0])
        else: data = np.array([], ndmin=2)
        return coo_matrix(data, dtype=self.dtype)

    # -----------------------------------------------------------------------------------------------------------------
    def copy(self):
        """ COPY METHOD

        :return: a Deep Copy of the GraphObject instance. """
        return GraphObject(nodes=self.getNodes(), arcs=self.getArcs(return_indices=True), targets=self.getTargets(),
                           set_mask=self.getSetMask(), output_mask=self.getOutputMask(),
                           sample_weight=self.getSampleWeights(), NodeGraph=self.getNodeGraph(),
                           aggregation_mode=self.aggregation_mode, dtype=np.dtype(self.dtype))

    # -----------------------------------------------------------------------------------------------------------------
    def __copy__(self):
        """ Copy inline method """
        return self.copy()

    # -----------------------------------------------------------------------------------------------------------------
    def __deepcopy__(self):
        """ Deep copy inline method """
        return self.copy()

    # -----------------------------------------------------------------------------------------------------------------
    def __eq__(self, g):
        """ Equality inline methods. Check equality between graphs, in lines like g1 == g2. """
        # check representation strings, as they are immediate to obtain and summarize graphs in the same way
        if str(self) != str(g): return False

        # if strings are the same, then check for all the quantities
        # Note that Adjacency and ArcNode are not checked,
        # since they are calculated from arcs indices and aggregation_mode
        self_data, g_data = self.get_dict_data(), g.get_dict_data()
        for key in self_data:
            if not np.array_equal(self_data[key], g_data[key]): return False

        return True

    ## REPRESENTATION METHODs #########################################################################################
    def __repr__(self):
        """ Representation string of the instance of GraphObject. """
        set_mask_type = 'all' if np.all(self.set_mask) else 'mixed'
        return f"graph(n={self.nodes.shape[0]}, a={self.arcs.shape[0]}, t={self.targets.shape[0]}, " \
               f"ndim={self.DIM_NODE_LABEL}, adim={self.DIM_ARC_LABEL}, tdim={self.DIM_TARGET}, " \
               f"set={set_mask_type}, mode={self.aggregation_mode}, dtype={self.dtype})"

    # -----------------------------------------------------------------------------------------------------------------
    def __str__(self):
        """ Representation string for the instance of GraphObject, for print() purpose. """
        return self.__repr__()

    ## SETTERS ########################################################################################################
    def setAggregation(self, aggregation_mode: str) -> None:
        """ Set ArcNode values for the specified :param aggregation_mode: """
        self.checkAggregation(aggregation_mode)
        self.aggregation_mode = aggregation_mode
        self.Adjacency = self.buildAdjacency(self.getArcs(return_indices=True)[:,:2])
        self.ArcNode = self.buildArcNode()

    # -----------------------------------------------------------------------------------------------------------------
    def todtype(self, dtype='float32'):
        """ cast GraphObject variables to :param dtype: dtype. """
        self.dtype = np.dtype(dtype)
        self.nodes = self.nodes.astype(dtype)
        self.arcs = self.arcs.astype(dtype)
        self.targets = self.targets.astype(dtype)
        self.sample_weight = self.sample_weight.astype(dtype)
        self.Adjacency = self.Adjacency.astype(dtype)
        self.ArcNode = self.ArcNode.astype(dtype)
        self.NodeGraph = self.NodeGraph.astype(dtype)

    ## NORMALIZERS ####################################################################################################
    def normalize(self, scalers: dict[dict], return_scalers: bool = False):
        """ Normalize GraphObject with an arbitrary scaler. Work well tith scikit-learn preprocessing scalers.

        :param scalers: (dict). Possible keys are ['nodes', 'arcs', 'targets']
                        scalers[key] is a dict with possible keys in ['class', 'kwargs']
                        scalers[key]['class'] is the scaler class of the arbitrary scaler
                        scalers[key]['kwargs'] are the keywords for fitting the arbitrary scaler on key data.
        :param return_scalers: (bool). If True, a dictionary scaler_dict is returned.
                               The output is a dict with possible keys in [nodes, arcs, targets].
                               If a scaler is missing, related key is not used.
                               For example, if scalers_kwargs.keys() in [['nodes','targets'], ['targets','nodes']],
                               the output is ad dict {'nodes': nodes_scaler, 'targets': target_scaler}. """

        # output scaler, if needed
        scalers_output_dict = dict()

        # normalize nodes
        if 'nodes' in scalers:
            node_scaler = scalers['nodes']['class'](**scalers['nodes']['kwargs'].get('nodes', dict())).fit(self.nodes)
            self.nodes = node_scaler.transform(self.nodes)
            scalers_output_dict['nodes'] = node_scaler

        # normalize arcs if arcs features are available
        if 'arcs' in scalers and self.DIM_ARC_LABEL > 0:
            arcs_scaler = scalers['arcs']['class'](**scalers['arcs']['kwargs'].get('arcs', dict())).fit(self.arcs)
            self.arcs = arcs_scaler.transform(self.arcs)
            scalers_output_dict['arcs'] = arcs_scaler

        # normalize targets
        if 'targets' in scalers:
            target_scaler = scalers['targets']['class'](**scalers['targets']['kwargs'].get('targets', dict())).fit(self.targets)
            self.targets = target_scaler.transform(self.targets)
            scalers_output_dict['targets'] = target_scaler

        if return_scalers:
            return scalers_output_dict

    # -----------------------------------------------------------------------------------------------------------------
    def normalize_from(self, nodes_scaler=None, arcs_scaler=None, targets_scaler=None):
        # normalize nodes
        if nodes_scaler is not None: self.nodes = nodes_scaler.transform(self.nodes)

        # normalize arcs if arcs features are available
        if arcs_scaler is not None and self.DIM_ARC_LABEL > 0: self.arcs = arcs_scaler.transform(self.arcs)

        # normalize targets
        if targets_scaler is not None: self.targets = targets_scaler.transform(self.targets)

    ## GETTERS ########################################################################################################
    # ALL return a deep copy of the corresponding element.
    def getArcs(self, return_indices=False):
        arcs = self.arcs.copy()
        if return_indices: arcs = np.hstack([np.array((self.Adjacency.row, self.Adjacency.col)).transpose(), arcs])
        return arcs

    def getNodes(self):
        return self.nodes.copy()

    def getTargets(self):
        return self.targets.copy()

    def getSetMask(self):
        return self.set_mask.copy()

    def getOutputMask(self):
        return self.output_mask.copy()

    def getAdjacency(self):
        return self.Adjacency.copy()

    def getArcNode(self):
        return self.ArcNode.copy()

    def getNodeGraph(self):
        return self.NodeGraph.copy()

    def getSampleWeights(self):
        return self.sample_weight.copy()

    def getDtype(self):
        return np.dtype(self.dtype)

    ## SAVER METHODs ##################################################################################################
    def get_dict_data(self):
        """ Return all useful elements for storing a graph :param g:, in a dict format. """

        # nodes, arcs and targets are saved by default.
        # arcs matrix are built so that each row contains [idx node outgoing arc, idx node ingoing arc, arc's features]
        data = {i: j for i, j in zip(['nodes', 'arcs', 'targets'],
                                     [self.nodes, self.getArcs(return_indices=True), self.targets])}

        # set_mask, output_mask and sample_weight are saved only in they have not all elements equal to 1.
        if not all(self.set_mask): data['set_mask'] = self.set_mask
        if not all(self.output_mask): data['output_mask'] = self.output_mask
        if np.any(self.sample_weight != 1): data['sample_weight'] = self.sample_weight

        # NodeGraph is saved only if it is a graph_focused problem and g is a merged graph resulting from GraphObject.merge.
        if (self.NodeGraph.size > 0 and self.NodeGraph.shape[1] > 1):
            data['NodeGraph'] = np.stack([self.NodeGraph.data, self.NodeGraph.row, self.NodeGraph.col]).transpose()

        return data

    # -----------------------------------------------------------------------------------------------------------------
    def save(self, graph_npz_path: str, **kwargs) -> None:
        """ Save graph in a .npz uncompressed archive.

        :param graph_npz_path: (str) path in which graph is saved.
        :param kwargs: kwargs argument of np.save function. """
        self.save_graph(graph_npz_path, self, False, **kwargs)

    # -----------------------------------------------------------------------------------------------------------------
    def save_compressed(self, graph_npz_path, **kwargs) -> None:
        """ Save graph in a .npz compressed archive.

        :param graph_npz_path: (str) path in which graph is saved.
        :param kwargs: kwargs argument of np.savez function. """
        self.save_graph(graph_npz_path, self, True, **kwargs)

    # -----------------------------------------------------------------------------------------------------------------
    def savetxt(self, graph_folder_path: str, format: str = '%.10g', **kwargs) -> None:
        """ Save graph in folder. All attributes are saved in as many .txt files as needed.

        :param graph_folder_path: (str) path in which graph is saved.
        :param kwargs: kwargs argument of np.savetxt function.
        """
        self.save_txt(graph_folder_path, self, format, **kwargs)

    ## STATIC METHODs ### SAVER #######################################################################################
    @staticmethod
    def save_graph(graph_npz_path: str, g, compressed: bool = False, **kwargs) -> None:
        """ Save a graph in a .npz compressed/uncompressed archive.

        :param graph_npz_path: path where a single .npz file will be stored, for saving the graph.
        :param g: graph of type GraphObject to be saved.
        :param compressed: bool, if True graph will be stored in a compressed npz file, npz uncompressed otherwise.
        :param kwargs: kwargs argument for for numpy.savez/numpy.savez_compressed function. """
        data = g.get_dict_data()
        saving_function = np.savez_compressed if compressed else np.savez
        saving_function(graph_npz_path, **data, **kwargs)

    # -----------------------------------------------------------------------------------------------------------------
    @staticmethod
    def save_txt(graph_folder_path: str, g, fmt: str = '%.10g', **kwargs) -> None:
        """ Save a graph to a directory, creating txt files referring to all needed attributes of graph g

        Note that graph_folder_path will contain ONLY a single graph g.
        If folder is not empty, it is removed and re-created.
        Remind that a generic dataset folder contains one folder for each graph.

        :param graph_folder_path: directory for saving the graph.
        :param g: graph of type GraphObject to be saved.
        :param fmt: param format passed to np.savetxt function.
        :param kwargs: kwargs argument of numpy.savetxt function. """

        # check folder.
        if graph_folder_path[-1] != '/': graph_folder_path += '/'
        if os.path.exists(graph_folder_path): shutil.rmtree(graph_folder_path)
        os.makedirs(graph_folder_path)

        data = g.get_dict_data()
        for i in data: np.savetxt(f"{graph_folder_path}{i}.txt", data[i], fmt=fmt, **kwargs)

    # -----------------------------------------------------------------------------------------------------------------
    @staticmethod
    def save_dataset(folder, glist, compressed=False, **kwargs) -> None:
        """ Save a dataset of graphs, in the form of a list of GraphObjects, in a folder.
        Each graph is saved as a npz file. Then the dataset is a folder of npz files.

        :param folder: (str) path for saving the dataset. If folder already exists, it is removed and re-created.
        :param glist: list of GraphObjects to be saved in the folder.
        :param compressed: (bool) if True every graph is saved in a npz compressed file, npz uncompressed otherwise.
        :param kwargs: keargs argument of np.save/np.savez functions. """
        if folder[-1] != '/': folder += '/'
        if os.path.exists(folder): shutil.rmtree(folder)
        os.makedirs(folder)
        for idx, g in enumerate(glist): GraphObject.save_graph(f"{folder}/g{idx}", g, compressed, **kwargs)

    # -----------------------------------------------------------------------------------------------------------------
    @staticmethod
    def save_dataset_txt(folder, glist, **kwargs) -> None:
        """ Save a dataset of graphs, in the form of a list of GraphObjects, in a folder.
         Each graph is saved as folder of txt files. Then the dataset is a folder of folders of txt files.

        :param folder: (str) path for saving the dataset. If folder already exists, it is removed and re-created.
        :param glist: list of GraphObjects to be saved in the folder.
        :param kwargs: kwargs argument of numpy.savetxt function. """
        if folder[-1] != '/': folder += '/'
        if os.path.exists(folder): shutil.rmtree(folder)
        os.makedirs(folder)
        for idx, g in enumerate(glist): GraphObject.save_txt(f"{folder}/g{idx}", g, **kwargs)

    ## STATIC METHODs ### UTILS #######################################################################################
    @staticmethod
    def checkAggregation(aggregation_mode):
        """ Check aggregation_mode parameter. Must be in ['average', 'sum', 'normalized'].

        :raise: Error if :param aggregation_mode: is not in ['average', 'sum', 'normalized']."""
        if str(aggregation_mode) not in ['sum', 'normalized', 'average']:
            raise ValueError("ERROR: Unknown aggregation mode")

    ## CLASS METHODs ### LOADER #######################################################################################
    @classmethod
    def load(cls, graph_npz_path, focus, aggregation_mode, **kwargs):
        """ Load a GraphObject from a npz compressed/uncompressed file.

        :param graph_npz_path: path to the npz graph file.
        :param focus: (str) 'n' node-focused; 'a' arc-focused; 'g' graph-focused. See __init__ for details.
        :param aggregation_mode: (str) incoming message aggregation mode. See BuildArcNode for details.
        :param kwargs: kwargs argument of numpy.load function. """
        if '.npz' not in graph_npz_path: graph_npz_path += '.npz'

        dtype = kwargs.pop('dtype', 'float32')
        data = dict(np.load(graph_npz_path, **kwargs))

        # Translate matrices from (length, 3) [values, index1, index2] to coo sparse matrices.
        nodegraph = data.pop('NodeGraph', None)
        if nodegraph is not None: data['NodeGraph'] = coo_matrix((nodegraph[:, 0], nodegraph[:, 1:].astype(int)))

        return cls(focus=focus, aggregation_mode=aggregation_mode, dtype=dtype, **data)

    # -----------------------------------------------------------------------------------------------------------------
    @classmethod
    def load_txt(cls, graph_folder_path: str, focus: str, aggregation_mode: str, **kwargs):
        """ Load a graph from a directory which contains at least 3 txt files referring to nodes, arcs and targets.

        :param graph_folder_path: directory containing at least 3 files: 'nodes.txt', 'arcs.txt' and 'targets.txt'
            > other possible files: 'NodeGraph.txt', 'output_mask.txt', 'set_mask.txt' and 'sample_weight.txt'.
            No other files required!
        :param focus: (str) 'n' node-focused; 'a' arc-focused; 'g' graph-focused. See __init__ for details.
            > NOTE  For graph-focused problems, file 'NodeGraph.txt' must to be present in folder.
            NodeGraph has shape (nodes, 3) s.t. in coo_matrix NodeGraph[:, 0]==data and NodeGraph[:, 1:]==indices for data.
        :param aggregation_mode: (str) incoming message aggregation mode. See BuildArcNode for details.
        :param kwargs: kwargs argument of numpy.loadtxt function.
        :return: GraphObject described by files in <graph_folder_path> folder. """

        # load all the files inside <graph_folder_path> folder.
        if graph_folder_path[-1] != '/': graph_folder_path += '/'

        files = os.listdir(graph_folder_path)
        dtype = kwargs.pop('dtype', 'float32')
        keys = [i.rsplit('.')[0] for i in files]
        vals = [np.loadtxt(graph_folder_path + i, ndmin=2, **kwargs) for i in files]

        # create a dictionary with parameters and values to be passed to GraphObject's constructor.
        data = dict(zip(keys, vals))

        # Translate matrices from (length, 3) [values, index1, index2] to coo sparse matrices.
        nodegraph = data.pop('NodeGraph', None)
        if nodegraph is not None: data['NodeGraph'] = coo_matrix((nodegraph[:, 0], nodegraph[:, 1:].astype(int)))

        return cls(focus=focus, aggregation_mode=aggregation_mode, dtype=dtype, **data)

    # -----------------------------------------------------------------------------------------------------------------
    @classmethod
    def load_dataset(cls, folder, focus, aggregation_mode, **kwargs):
        """ Load a dataset of graphs stored in a folder of npz graph files.
        To be used after save_dataset method.

        :param folder: path to the folder where npz graph files are stored.
        :param focus: (str) 'n' node-focused; 'a' arc-focused; 'g' graph-focused. See __init__ for details.
        :param aggregation_mode: (str) incoming message aggregation mode. See BuildArcNode for details.
        :param kwargs: kwargs argument of numpy.load function.
        :return: a list of GraphObject elements. """
        return [cls.load(f"{folder}/{g}", focus, aggregation_mode, **kwargs) for g in os.listdir(folder)]

    # -----------------------------------------------------------------------------------------------------------------
    @classmethod
    def load_dataset_txt(cls, folder, focus, aggregation_mode, **kwargs):
        """ Load a dataset of graphs stored in a folder of graph folders.
        To be used after save_dataset_txt method.

        :param folder: path to the folder where graph folders are stored.
        :param focus: (str) 'n' node-focused; 'a' arc-focused; 'g' graph-focused. See __init__ for details.
            > NOTE  For graph-focused problems, file 'NodeGraph.txt' must to be present in folders.
            NodeGraph has shape (nodes, 3) s.t. in coo_matrix NodeGraph[:, 0]==data and NodeGraph[:, 1:]==indices for data.
        :param aggregation_mode: (str) incoming message aggregation mode. See BuildArcNode for details.
        :param kwargs: kwargs argument of numpy.loadtxt function.
        :return: a list of GraphObject elements. """
        return [cls.load_txt(f"{folder}/{g}", focus, aggregation_mode, **kwargs) for g in os.listdir(folder)]

    ## CLASS METHODs ### MERGER #######################################################################################
    @classmethod
    def merge(cls, glist: list, focus: str, aggregation_mode: str, dtype='float32'):
        """ Method to merge a list of GraphObject elements in a single GraphObject element.

        :param glist: list of GraphObject elements to be merged.
            > NOTE if focus=='g', new NodeGraph will have dimension (Num nodes, Num graphs).
        :param aggregation_mode: (str) incoming message aggregation mode. See BuildArcNode for details.
        :param dtype: dtype of elements of new arrays after merging procedure.
        :return: a new GraphObject containing all the information (nodes, arcs, targets, ...) in glist. """
        get_data = lambda x: [(i.getNodes(), i.nodes.shape[0], i.getArcs(return_indices=True),
                               i.getTargets(), i.getSetMask(), i.getOutputMask(),
                               i.getSampleWeights(), i.getNodeGraph()) for i in x]
        nodes, nodes_lens, arcs, targets, set_mask, output_mask, sample_weight, nodegraph_list = zip(*get_data(glist))

        # get single matrices for new graph
        nodes = np.concatenate(nodes, axis=0, dtype=dtype)
        for i, elem in enumerate(arcs): elem[:, :2] += sum(nodes_lens[:i])
        arcs = np.concatenate(arcs, axis=0, dtype=dtype)
        targets = np.concatenate(targets, axis=0, dtype=dtype)
        set_mask = np.concatenate(set_mask, axis=0, dtype=bool)
        output_mask = np.concatenate(output_mask, axis=0, dtype=bool)
        sample_weight = np.concatenate(sample_weight, axis=0, dtype=dtype)

        from scipy.sparse import block_diag
        nodegraph = block_diag(nodegraph_list, dtype=dtype)

        # resulting GraphObject.
        return GraphObject(nodes=nodes, arcs=arcs, targets=targets, focus=focus,
                           set_mask=set_mask, output_mask=output_mask, sample_weight=sample_weight,
                           NodeGraph=nodegraph, aggregation_mode=aggregation_mode, dtype=dtype)

    ## CLASS METHODs ### UTILS ########################################################################################
    @classmethod
    def fromGraphTensor(cls, g, focus: str, dtype='float32'):
        """ Create GraphObject from GraphTensor.

        :param g: a GraphTensor element to be translated into a GraphObject element.
        :param focus: (str) 'n' node-focused; 'a' arc-focused; 'g' graph-focused. See __init__ for details.
        :return: a GraphObject element whose tensor representation is g.
        """
        nodegraph = coo_matrix((g.NodeGraph.values, tf.transpose(g.NodeGraph.indices))) if focus == 'g' else None
        return cls(nodes=g.nodes.numpy(), arcs=np.hstack([g.Adjacency.indices, g.arcs.numpy()]), targets=g.targets.numpy(),
                   set_mask=g.set_mask.numpy(), output_mask=g.output_mask.numpy(), sample_weight=g.sample_weight.numpy(),
                   NodeGraph=nodegraph, aggregation_mode=g.aggregation_mode, focus=focus, dtype=dtype)


#######################################################################################################################
## GRAPH TENSOR CLASS #################################################################################################
#######################################################################################################################
class GraphTensor:
    """ Tensor version of a GraphObject. Useful to speed up learning processes. """

    ## CONSTRUCTORS METHODs ###########################################################################################
    def __init__(self, nodes, dim_node_label, arcs, targets, set_mask, output_mask, sample_weight,
                 Adjacency, ArcNode, NodeGraph, aggregation_mode, dtype):
        """ It contains all information to be passed to GNN model,
        but described with tensorflow dense/sparse tensors. """

        self.dtype = tf.as_dtype(dtype)
        self.aggregation_mode = aggregation_mode

        # store dimensions: first two columns of arcs contain nodes indices.
        self.DIM_ARC_LABEL = arcs.shape[1]
        self.DIM_TARGET = targets.shape[1]

        # constant dense tensors.
        self.DIM_NODE_LABEL = tf.constant(dim_node_label, dtype=tf.int32)
        self.nodes = tf.constant(nodes, dtype=self.dtype)
        self.arcs = tf.constant(arcs, dtype=self.dtype)
        self.targets = tf.constant(targets, dtype=self.dtype)
        self.sample_weight = tf.constant(sample_weight, dtype=self.dtype)
        self.set_mask = tf.constant(set_mask, dtype=bool)
        self.output_mask = tf.constant(output_mask, dtype=bool)

        # sparse tensors.
        self.Adjacency = tf.sparse.SparseTensor.from_value(Adjacency)
        self.ArcNode = tf.sparse.SparseTensor.from_value(ArcNode)
        self.NodeGraph = tf.sparse.SparseTensor.from_value(NodeGraph)

    # -----------------------------------------------------------------------------------------------------------------
    def copy(self):
        """ COPY METHOD

        :return: a Deep Copy of the GraphTensor instance. """
        return GraphTensor(nodes=self.nodes, dim_node_label=self.DIM_NODE_LABEL, arcs=self.arcs, targets=self.targets,
                           set_mask=self.set_mask, output_mask=self.output_mask, sample_weight=self.sample_weight,
                           Adjacency=self.Adjacency, ArcNode=self.ArcNode, NodeGraph=self.NodeGraph,
                           aggregation_mode=self.aggregation_mode, dtype=self.dtype)

    ## REPRESENTATION METHODs #########################################################################################
    def __repr__(self):
        """ Representation string for the instance of GraphTensor """
        set_mask_type = 'all' if tf.reduce_all(self.set_mask) else 'mixed'
        return f"graph_tensor(n={self.nodes.shape[0]}, a={self.arcs.shape[0]}, " \
               f"ndim={self.DIM_NODE_LABEL}, adim={self.DIM_ARC_LABEL}, tdim={self.DIM_TARGET}, " \
               f"set={set_mask_type}, mode={self.aggregation_mode}, dtype={self.dtype})"

    # -----------------------------------------------------------------------------------------------------------------
    def __str__(self):
        """ Representation string for the instance of GraphTensor, for print() purpose. """
        return self.__repr__()

    ## SAVER METHODs ##################################################################################################
    def save(self, graph_npz_path, **kwargs) -> None:
        """ Save graph in a .npz uncompressed archive.

        :param graph_npz_path: (str) path in which graph is saved.
        :param kwargs: kwargs argument of np.save function. """
        self.save_graph(graph_npz_path, self, False, **kwargs)

    # -----------------------------------------------------------------------------------------------------------------
    def save_compressed(self, graph_npz_path, **kwargs) -> None:
        """ Save graph in a .npz compressed archive.

        :param graph_npz_path: (str) path in which graph is saved.
        :param kwargs: kwargs argument of np.savez function. """
        self.save_graph(graph_npz_path, self, True, **kwargs)

    ## STATIC METHODs ### SAVER #######################################################################################
    @staticmethod
    def save_graph(graph_npz_path: str, g, compressed: bool = False, **kwargs) -> None:
        """ Save a graph in a .npz compressed/uncompressed archive.

        :param graph_npz_path: path where a single .npz file will be stored, for saving the graph.
        :param g: graph of type GraphObject to be saved.
        :param compressed: bool, if True graph will be stored in a compressed npz file, npz uncompressed otherwise.
        :param kwargs: kwargs argument for for numpy.savez/numpy.savez_compressed function. """
        sparse_data = {'aggregation_mode': np.array(g.aggregation_mode)}
        for i, mat in zip(['Adjacency', 'ArcNode', 'NodeGraph'], [g.Adjacency, g.ArcNode, g.NodeGraph]):
            sparse_data[i] = tf.concat([mat.values[:, tf.newaxis], tf.cast(mat.indices, g.dtype)], axis=1)
            sparse_data[i + '_shape'] = mat.shape

        saving_function = np.savez_compressed if compressed else np.savez
        saving_function(graph_npz_path, dim_node_label=g.DIM_NODE_LABEL,
                        nodes=g.nodes, arcs=g.arcs, targets=g.targets, sample_weight=g.sample_weight,
                        set_mask=g.set_mask, output_mask=g.output_mask, dtype=g.dtype, **sparse_data, **kwargs)

    ## CLASS METHODs ### LOADER #######################################################################################
    @classmethod
    def load(cls, graph_npz_path, **kwargs):
        """ Load a GraphTensor from a npz compressed/uncompressed file.

        :param graph_npz_path: path to the npz graph file.
        :param kwargs: kwargs argument of numpy.load function. """
        if '.npz' not in graph_npz_path: graph_npz_path += '.npz'
        dtype = kwargs.pop('dtype', 'tf.float32')
        data = dict(np.load(graph_npz_path, **kwargs))

        data['aggregation_mode'] = str(data['aggregation_mode'])
        for i in ['Adjacency', 'ArcNode', 'NodeGraph']:
            data[i] = tf.SparseTensor(indices=data[i][:, 1:], values=data[i][:, 0], dense_shape=data.pop(i + '_shape'))

        return cls(**data)

    ## CLASS and STATIC METHODs ### UTILS #############################################################################
    @classmethod
    def fromGraphObject(cls, g: GraphObject):
        """ Create GraphTensor from GraphObject.

        :param g: a GraphObject element to be translated into a GraphTensor element.
        :return: a GraphTensor element whose normal representation is g. """
        return cls(nodes=g.nodes, dim_node_label=g.DIM_NODE_LABEL, arcs=g.arcs, targets=g.targets,
                   set_mask=g.set_mask, output_mask=g.output_mask, sample_weight=g.sample_weight,
                   NodeGraph=cls.COO2SparseTensor(g.NodeGraph), Adjacency=cls.COO2SparseTensor(g.Adjacency),
                   ArcNode=cls.COO2SparseTensor(g.ArcNode), aggregation_mode=g.aggregation_mode, dtype=g.dtype)

    # -----------------------------------------------------------------------------------------------------------------
    @staticmethod
    def COO2SparseTensor(coo_matrix) -> tf.Tensor:
        """ Get the sparse tensor from a sparse :param coo_matrix: matrix. """
        indices = np.zeros(shape=(0, 2), dtype=int)
        if coo_matrix.size > 0: indices = list(zip(coo_matrix.row, coo_matrix.col))

        # SparseTensor is created and then reordered to be correctly computable. NOTE: reorder() recommended by TF2.0+.
        sparse_tensor = tf.SparseTensor(indices=indices, values=coo_matrix.data, dense_shape=coo_matrix.shape)
        sparse_tensor = tf.sparse.reorder(sparse_tensor)
        sparse_tensor = tf.cast(sparse_tensor, dtype=coo_matrix.dtype)
        return sparse_tensor
