import argparse

import dask
import dask.array as da
import montblanc

def create_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("ms", help="Measurement Set", type=str)
    return parser

# Parse command line arguments
args = create_parser().parse_args()

# Create a montblanc dataset from the xarray dataset
mds = montblanc.dataset_from_ms(args.ms)
mds = montblanc.montblanc_dataset(mds)
# Rechunk the dataset so that a tile of the problem fits within 1GB
mds = montblanc.rechunk_to_budget(mds, 128*1024*1024)

# Create a rime solver
rime = montblanc.Rime(cfg={'dtype':'double'})

# Get a dask expression for the model visibilities, given the input dataset
mds = mds.assign(data=mds.corrected_data)
mds = mds.persist()
model_vis, chi_squared = rime(mds)

# Evaluate the expression
print dask.compute(model_vis, chi_squared)