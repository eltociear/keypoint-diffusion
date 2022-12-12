from pathlib import Path
import pickle
from typing import Dict, List, Union

import dgl
from dgl.dataloading import GraphDataLoader
import torch

from data_processing.pdbbind_processing import (build_receptor_graph,
                                                get_pocket_atoms, parse_ligand,
                                                parse_protein, get_ot_loss_weights)

# TODO: figure out what ligand atom elements we would/should actually support. We don't really need to include the metals do we?

class CrossDockedDataset(dgl.data.DGLDataset):

    def __init__(self, name: str, 
        processed_data_file: str,
        rec_elements: List[str],
        lig_elements: List[str],
        pocket_edge_algorithm: str = 'bruteforce-blas',
        lig_box_padding: Union[int, float] = 6,
        pocket_cutoff: Union[int, float] = 4,
        receptor_k: int = 3,
        use_boltzmann_ot: bool = False, **kwargs):

        # define filepath of data
        self.data_file: Path = Path(processed_data_file)

        # atom typing configurations
        self.rec_elements = rec_elements
        self.rec_element_map: Dict[str, int] = { element: idx for idx, element in enumerate(self.rec_elements) }
        self.rec_element_map['other'] = len(self.rec_elements)

        self.lig_elements = lig_elements
        self.lig_element_map: Dict[str, int] = { element: idx for idx, element in enumerate(self.lig_elements) }
        self.lig_element_map['other'] = len(self.lig_elements)

        # hyperparameters for protein graph
        self.receptor_k: int = receptor_k
        self.lig_box_padding: Union[int, float] = lig_box_padding
        self.pocket_cutoff: Union[int, float] = pocket_cutoff
        self.pocket_edge_algorithm: str = pocket_edge_algorithm

        self.use_boltzmann_ot = use_boltzmann_ot

        super().__init__(name=name) # this has to happen last because this will call self.process()

    def __getitem__(self, i):
        data_dict = self.data[i]
        return data_dict['receptor_graph'], data_dict['lig_atom_positions'], data_dict['lig_atom_features']

    def __len__(self):
        return len(self.data)

    def process(self):
        # load data into memory
        with open(self.data_file, 'rb') as f:
            self.data = pickle.load(f)

        
def collate_fn(examples: list):

    # break receptor graphs, ligand positions, and ligand features into separate lists
    receptor_graphs, lig_atom_positions, lig_atom_features = zip(*examples)

    # batch the receptor graphs together
    receptor_graphs = dgl.batch(receptor_graphs)
    return receptor_graphs, lig_atom_positions, lig_atom_features

def get_dataloader(dataset: CrossDockedDataset, batch_size: int, num_workers: int = 1) -> GraphDataLoader:

    dataloader = GraphDataLoader(dataset, batch_size=batch_size, drop_last=False, num_workers=num_workers, collate_fn=collate_fn)
    return dataloader
