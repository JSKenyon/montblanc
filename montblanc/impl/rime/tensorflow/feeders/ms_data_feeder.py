#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2015 Simon Perkins
#
# This file is part of montblanc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>.


import collections
import functools
import types

import numpy as np

from hypercube import HyperCube
import pyrap.tables as pt

import montblanc.util as mbu
from montblanc.config import RimeSolverConfig as Options

from rime_data_feeder import RimeDataFeeder

# Map MS column string types to numpy types
MS_TO_NP_TYPE_MAP = {
    'INT' : np.int32,
    'FLOAT' : np.float32,
    'DOUBLE' : np.float64,
    'BOOLEAN' : np.bool,
    'COMPLEX' : np.complex64,
    'DCOMPLEX' : np.complex128
}

FLOAT_TO_DOUBLE_CAST_MAP = {
    'COMPLEX' : 'DCOMPLEX',
    'FLOAT' : 'DOUBLE',
}

DOUBLE_TO_FLOAT_CAST_MAP = { v: k for
    k, v in FLOAT_TO_DOUBLE_CAST_MAP.iteritems() }


# Key names for main and taql selected tables
MAIN_TABLE = 'MAIN'
ORDERED_MAIN_TABLE = 'ORDERED_MAIN'
ORDERED_UVW_TABLE = 'ORDERED_UVW'
ORDERED_TIME_TABLE = 'ORDERED_TIME'
ORDERED_BASELINE_TABLE = 'ORDERED_BASELINE'

# Measurement Set sub-table name string constants
ANTENNA_TABLE = 'ANTENNA'
SPECTRAL_WINDOW_TABLE = 'SPECTRAL_WINDOW'
DATA_DESCRIPTION_TABLE = 'DATA_DESCRIPTION'
POLARIZATION_TABLE = 'POLARIZATION'
FIELD_TABLE = 'FIELD'

SUBTABLE_KEYS = (ANTENNA_TABLE,
    SPECTRAL_WINDOW_TABLE,
    DATA_DESCRIPTION_TABLE,
    POLARIZATION_TABLE,
    FIELD_TABLE)

# Main MS column name constants
TIME = 'TIME'
ANTENNA1 = 'ANTENNA1'
ANTENNA2 = 'ANTENNA2'
UVW = 'UVW'
DATA = 'DATA'
FLAG = 'FLAG'
WEIGHT = 'WEIGHT'

# Antenna sub-table column name constants
POSITION = 'POSITION'

# Field sub-table column name constants
PHASE_DIR = 'PHASE_DIR'

# Columns used in select statement
SELECTED = [TIME, ANTENNA1, ANTENNA2, UVW, DATA, FLAG, WEIGHT]

# Named tuple defining a mapping from MS row to dimension
OrderbyMap = collections.namedtuple("OrderbyMap", "dimension orderby")

# Mappings for time, baseline and band
TIME_MAP = OrderbyMap("ntime", "TIME")
BASELINE_MAP = OrderbyMap("nbl", "ANTENNA1, ANTENNA2")
BAND_MAP = OrderbyMap("nbands", "[SELECT SPECTRAL_WINDOW_ID "
        "FROM ::DATA_DESCRIPTION][DATA_DESC_ID]")

# Place mapping in a list
MS_ROW_MAPPINGS = [
    TIME_MAP,
    BASELINE_MAP,
    BAND_MAP
]

def cache_ms_read(method):
    """
    Decorator for caching MSRimeDataFeeder feeder function return values

    Create a key index for the proxied array in the FeedContext.
    Iterate over the array shape descriptor e.g. (ntime, nbl, 3)
    returning tuples containing the lower and upper extents
    of string dimensions. Takes (0, d) in the case of an integer
    dimensions.
    """

    @functools.wraps(method)
    def memoizer(self, context):
        D = context.dimensions(copy=False)
        # (lower, upper) else (0, d)
        idx = ((D[d].lower_extent, D[d].upper_extent) if d in D
            else (0, d) for d in context.array(context.name).shape)
        # Construct the key for the above index
        key = tuple(i for t in idx for i in t)
        # Access the sub-cache for this array
        array_cache = self._cache[context.name]

        # Cache miss, call the function
        if key not in array_cache:
            array_cache[key] = method(self, context)

        return array_cache[key]

    return memoizer

def orderby_clause(dimensions, unique=False):
    columns = ", ".join(m.orderby for m
        in MS_ROW_MAPPINGS if m.dimension in dimensions)

    return " ".join(("ORDERBY", "UNIQUE" if unique else "", columns))

def select_columns(dimensions, dtypes, precision=None):
    """
    Generate select columns. columns will be casted according
    specified precision
    """
    if precision is None or precision == Options.DTYPE_DOUBLE:
        dtypes = [FLOAT_TO_DOUBLE_CAST_MAP.get(d, d) for d in dtypes]
    elif precision == Options.DTYPE_FLOAT:
        dtypes = [DOUBLE_TO_FLOAT_CAST_MAP.get(d, d) for d in dtypes]
    else:
        raise ValueError("Invalid precision '{p}'".format(p=precision))

    return ", ".join('{n} AS {n} {d}'.format(n=n, d=d)
        for n, d in zip(dimensions, dtypes))

def subtable_name(msname, subtable=None):
    return '::'.join((msname, subtable)) if subtable else msname

def open_table(msname, subtable=None):
    return pt.table(subtable_name(msname, subtable), ack=False)

class MSRimeDataFeeder(RimeDataFeeder):
    # Main measurement set ordering dimensions
    MS_DIM_ORDER = ('ntime', 'nbl', 'nbands')
    # UVW measurement set ordering dimensions
    UVW_DIM_ORDER = ('ntime', 'nbl')

    def __init__(self, msname, precision=None):
        super(MSRimeDataFeeder, self).__init__()

        if precision is None:
            precision = Options.DTYPE_DOUBLE

        self._msname = msname
        # Create dictionary of tables
        self._tables = { k: open_table(msname, k) for k in SUBTABLE_KEYS }
        self._cube = cube = HyperCube()

        # Open the main measurement set
        ms = open_table(msname)

        # Access individual tables
        ant, spec, ddesc, pol, field = (self._tables[k] for k in SUBTABLE_KEYS)

        # Sanity check the polarizations
        if pol.nrows() > 1:
            raise ValueError("Multiple polarization configurations!")

        npol = pol.getcol('NUM_CORR')[0]

        if npol != 4:
            raise ValueError('Expected four polarizations')

        # Number of channels per band
        chan_per_band = spec.getcol('NUM_CHAN')

        # Require the same number of channels per band
        if not all(chan_per_band[0] == cpb for cpb in chan_per_band):
            raise ValueError('Channels per band {cpb} are not equal!'
                .format(cpb=chan_per_band))

        if ddesc.nrows() != spec.nrows():
            raise ValueError("DATA_DESCRIPTOR.nrows() "
                "!= SPECTRAL_WINDOW.nrows()")

        # Hard code auto-correlations and field_id 0
        auto_correlations = True
        field_id = 0

        select_cols = select_columns(SELECTED,
            [ms.getcoldesc(c)["valueType"].upper() for c in SELECTED],
            precision=precision)

        # Create a view over the MS, ordered by
        # (1) time (TIME)
        # (2) baseline (ANTENNA1, ANTENNA2)
        # (3) band (SPECTRAL_WINDOW_ID via DATA_DESC_ID)
        ordering_query = " ".join((
            "SELECT {c} FROM $ms".format(c=select_cols),
            "WHERE FIELD_ID={fid}".format(fid=field_id),
            "" if auto_correlations else "AND ANTENNA1 != ANTENNA2",
            orderby_clause(self.MS_DIM_ORDER)
        ))

        # Ordered Measurement Set
        oms = pt.taql(ordering_query)
        # Measurement Set ordered by unique time and baseline
        otblms = pt.taql("SELECT FROM $oms {c}".format(
            c=orderby_clause(self.UVW_DIM_ORDER, unique=True)))

        # Store the main table
        self._tables[MAIN_TABLE] = ms
        self._tables[ORDERED_MAIN_TABLE] = oms
        self._tables[ORDERED_UVW_TABLE] = otblms

        # Count distinct timesteps in the MS
        t_orderby = orderby_clause(['ntime'], unique=True)
        t_query = "SELECT FROM $otblms {c}".format(c=t_orderby)
        self._tables[ORDERED_TIME_TABLE] = ot = pt.taql(t_query)
        ntime = ot.nrows()

        # Count number of baselines in the MS
        bl_orderby = orderby_clause(['nbl'], unique=True)
        bl_query = "SELECT FROM $otblms {c}".format(c=bl_orderby)
        self._tables[ORDERED_BASELINE_TABLE] = obl = pt.taql(bl_query)
        nbl = obl.nrows()

        # Cache columns on the object
        # Handle these columns slightly differently
        # They're used to compute the parallactic angle
        # TODO: Fit them into the cache_ms_read strategy at some point

        # Cache antenna positions
        self._antenna_positions = ant.getcol(POSITION)

        # Cache timesteps
        self._times = ot.getcol(TIME)

        # Cache the phase direction for the field
        self._phase_dir = field.getcol(PHASE_DIR, startrow=field_id, nrow=1)[0][0]

        # Register dimensions on the cube
        cube.register_dimension('npol', npol,
            description='Polarisations')
        cube.register_dimension('nbands', len(chan_per_band),
            description='Bands')
        cube.register_dimension('nchan', sum(chan_per_band),
            description='Channels')
        cube.register_dimension('nchanperband', chan_per_band[0],
            description='Channels-per-band')
        cube.register_dimension('nrows', ms.nrows(),
            description='Main MS rows')
        cube.register_dimension('nuvwrows', otblms.nrows(),
            description='UVW sub-MS rows')
        cube.register_dimension('na', ant.nrows(),
            description='Antenna')
        cube.register_dimension('ntime', ntime,
            description='Timesteps')
        cube.register_dimension('nbl', nbl,
            description='Baselines')

        def _cube_row_update_function(self):
            # Update main measurement set rows
            shape = self.dim_global_size(*MSRimeDataFeeder.MS_DIM_ORDER)
            lower = self.dim_lower_extent(*MSRimeDataFeeder.MS_DIM_ORDER)
            upper = tuple(u-1 for u in self.dim_upper_extent(
                *MSRimeDataFeeder.MS_DIM_ORDER))

            self.update_dimension(name='nrows',
                lower_extent=np.ravel_multi_index(lower, shape),
                upper_extent=np.ravel_multi_index(upper, shape)+1)

            shape = self.dim_global_size(*MSRimeDataFeeder.UVW_DIM_ORDER)
            lower = self.dim_lower_extent(*MSRimeDataFeeder.UVW_DIM_ORDER)
            upper = tuple(u-1 for u in self.dim_upper_extent(
                *MSRimeDataFeeder.UVW_DIM_ORDER))

            self.update_dimension(name='nuvwrows',
                lower_extent=np.ravel_multi_index(lower, shape),
                upper_extent=np.ravel_multi_index(upper, shape)+1)

        self._cube.update_row_dimensions = types.MethodType(
            _cube_row_update_function, self._cube)

        self._cache = collections.defaultdict(dict)

        # Temporary, need to get these arrays from elsewhere
        cube.register_array('uvw', ('ntime', 'na', 3), np.float64)
        cube.register_array('antenna1', ('ntime', 'nbl'), np.int32)
        cube.register_array('antenna2', ('ntime', 'nbl'), np.int32)
        cube.register_array('observed_vis', ('ntime', 'nbl', 'nchan', 'npol'), np.complex64)
        cube.register_array('weight', ('ntime', 'nbl', 'nchan', 'npol'), np.float32)
        cube.register_array('flag', ('ntime', 'nbl', 'nchan', 'npol'), np.bool)
        cube.register_array('parallactic_angles', ('ntime', 'na'), np.float64)

    @property
    def mscube(self):
        return self._cube

    @cache_ms_read
    def uvw(self, context):
        """ Special case for handling antenna uvw code """

        # Antenna reading code expects (ntime, nbl) ordering
        if self.UVW_DIM_ORDER != ('ntime', 'nbl'):
            raise ValueError("'{o}'' ordering expected for "
                "antenna reading code.".format(o=UVW_DIM_ORDER))

        (t_low, t_high) = context.dim_extents('ntime')
        na = context.dim_global_size('na')

        # We expect to handle all antenna at once
        if context.shape != (t_high - t_low, na, 3):
            raise ValueError("Received an unexpected shape "
                "{s} in (ntime,na,3) antenna reading code".format(
                    s=context.shape))

        # Create per antenna UVW coordinates.
        # u_01 = u_1 - u_0
        # u_02 = u_2 - u_0
        # ...
        # u_0N = u_N - U_0
        # where N = na - 1.

        # Choosing u_0 = 0 we have:
        # u_1 = u_01
        # u_2 = u_02
        # ...
        # u_N = u_0N

        # Then, other baseline values can be derived as
        # u_21 = u_1 - u_2

        # Allocate space for per-antenna UVW
        ant_uvw = np.empty(shape=context.shape, dtype=context.dtype)
        # Zero antenna 0
        ant_uvw[:,0,:] = 0

        # Read in uvw[1:na] row at each timestep
        for ti, t in enumerate(xrange(t_low, t_high)):
            # Inspection confirms that this achieves the# same effect as
            # ant_uvw[ti,1:na,:] = ...getcol(UVW, ...).reshape(na-1, -1)
            self._tables[ORDERED_UVW_TABLE].getcolnp(UVW,
                ant_uvw[ti,1:na,:],
                startrow=t*na+1, nrow=na-1)

        return ant_uvw

    @cache_ms_read
    def antenna1(self, context):
        lrow, urow = context.dim_extents('nuvwrows')
        antenna1 = self._tables[ORDERED_UVW_TABLE].getcol(
            ANTENNA1, startrow=lrow, nrow=urow-lrow)

        return antenna1.reshape(context.shape)

    @cache_ms_read
    def antenna2(self, context):
        lrow, urow = context.dim_extents('nuvwrows')
        antenna2 = self._tables[ORDERED_UVW_TABLE].getcol(
            ANTENNA2, startrow=lrow, nrow=urow-lrow)

        return antenna2.reshape(context.shape)

    @cache_ms_read
    def parallactic_angles(self, context):
        # Time and antenna extents
        (lt, ut), (la, ua) = context.dim_extents('ntime', 'na')

        return mbu.parallactic_angles(self._phase_dir,
            self._antenna_positions[la:ua],
            self._times[lt:ut])

    @cache_ms_read
    def observed_vis(self, context):
        lrow, urow = context.dim_extents('nrows')

        data = self._tables[ORDERED_MAIN_TABLE].getcol(
            DATA, startrow=lrow, nrow=urow-lrow)

        return data.reshape(context.shape)

    @cache_ms_read
    def flag(self, context):
        lrow, urow = context.dim_extents('nrows')

        flag = self._tables[ORDERED_MAIN_TABLE].getcol(
            FLAG, startrow=lrow, nrow=urow-lrow)

        return flag.reshape(context.shape)

    @cache_ms_read
    def weight(self, context):
        lrow, urow = context.dim_extents('nrows')
        nchan = context.dim_extent_size('nchanperband')

        weight = self._tables[ORDERED_MAIN_TABLE].getcol(
            WEIGHT, startrow=lrow, nrow=urow-lrow)

        # WEIGHT is applied across all channels
        weight = np.repeat(weight, nchan, 0)
        return weight.reshape(context.shape)

    def clear_cache(self):
        self._cache.clear()

    def close(self):
        self.clear_cache()

        for table in self._tables.itervalues():
            table.close()

    def __enter__(self):
        return self

    def __exit__(self, etype, evalue, etraceback):
        self.close()