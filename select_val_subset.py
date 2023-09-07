from pathlib import Path
import argparse
import random
import pickle
from typing import List
from rdkit import Chem

from Bio.PDB import PDBParser

def diffsbdd_can_read(data_dir: Path, ligand_file: Path):

    ligand_name = ligand_file.stem
    split_lig_name = ligand_name.split('_')
    pdb_file_name = "_".join(split_lig_name[:2])
    pdb_file = data_dir / f'{pdb_file_name}.pdb'

    txt_file = Path(data_dir, f"{ligand_name}.txt")

    with open(txt_file, 'r') as f:
        pocket_ids = f.read().split()

    try:

        pdb_struct = PDBParser(QUIET=True).get_structure('', pdb_file)[0]
        residues = [
            pdb_struct[x.split(':')[0]][(' ', int(x.split(':')[1]), ' ')]
            for x in pocket_ids]
    except Exception as e:
        print(e)
        # print('failure')
        return False
    
    return True

def filter_ligand_sizes(ligand_files: List[Path], min_lig_size: int, max_lig_size: int):

    if min_lig_size is None and max_lig_size is None:
        return ligand_files
    elif min_lig_size is None and max_lig_size is not None:
        min_lig_size = 0
    elif min_lig_size is not None and max_lig_size is None:
        max_lig_size = 1000
    
    filtered_ligand_files = []
    for ligand_file in ligand_files:

        # read ligand file into rdkit molecule
        mol = Chem.MolFromMolFile(str(ligand_file), sanitize=False)

        # get number of heavy atoms
        n_atoms = mol.GetNumAtoms()

        if n_atoms >= min_lig_size and n_atoms <= max_lig_size:
            filtered_ligand_files.append(ligand_file)
    return filtered_ligand_files

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument('ligdiff_data_dir', type=Path)
    p.add_argument('diffsbdd_fullatom_data', type=Path)
    p.add_argument('diffsbdd_ca_data', type=Path)
    p.add_argument('--n_pockets', type=int, default=50)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--min_lig_size', type=int, default=None)
    p.add_argument('--max_lig_size', type=int, default=None)
    p.add_argument('--ligdiff_file', type=Path, default=Path('val_subset/val_idxs.pkl'))
    p.add_argument('--diffsbdd_file', type=Path, default=Path('val_subset/val_files.txt'))

    args = p.parse_args()

    random.seed(args.seed)

    structures_dir = args.ligdiff_data_dir / 'val_structures'
    ligand_files = list(structures_dir.glob('[!.]*.sdf'))
    ligand_files = [ Path(x) for x in ligand_files ]
    ligand_files = filter_ligand_sizes(ligand_files, args.min_lig_size, args.max_lig_size)

    selected_ligand_files = random.sample(ligand_files, args.n_pockets+10)

    # confirm all selected ligand files can be read
    selected_ligand_files = [ x for x in selected_ligand_files if diffsbdd_can_read(args.diffsbdd_fullatom_data, x) and diffsbdd_can_read(args.diffsbdd_ca_data, x) ]
    if len(selected_ligand_files) > args.n_pockets:
        selected_ligand_files = selected_ligand_files[:args.n_pockets]
    
    ligand_names = [ x.stem for x in selected_ligand_files ]

    # ligand idxs in the dataset
    filenames_file = args.ligdiff_data_dir / 'val_filenames.pkl'
    with open(filenames_file, 'rb') as f:
        filenames_dict = pickle.load(f)

    # get the dataset index for every selected pocket
    selected_idxs = []
    for dataset_idx, lig_file in enumerate(filenames_dict['lig_files']):
        if Path(lig_file).stem in ligand_names:
            selected_idxs.append(dataset_idx)

    assert len(selected_idxs) == len(selected_ligand_files)

    with open(args.ligdiff_file, 'wb') as f:
        pickle.dump(selected_idxs, f)

    diffsbdd_str = ','.join([ Path(x).stem for x in selected_ligand_files ])
    with open(args.diffsbdd_file, 'w') as f:
        f.write(diffsbdd_str)
    
    
