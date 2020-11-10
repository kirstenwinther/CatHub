import re
import json
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine 
from ase.db import connect
from ase.data import chemical_symbols, atomic_numbers
from tabulate import tabulate


def db_to_dataframe(table_name, filename):
    "Read cathub .db file into pandas dataframe"

    # define sql url
    sql_url = 'sqlite:///' + str(filename)

    # SQLAlchemy connectable
    cnx = create_engine(sql_url).connect()

    # table will be returned as a dataframe
    df = pd.read_sql_table(table_name, cnx)
    return df

def write_energies(db_filepath, reference_gases, dummy_gases, dft_corrections_gases, beef_dft_helmholtz_offset, field_effects, adsorbate_parameters, write_gases, write_adsorbates, verbose=True):

    df_out = pd.DataFrame(columns=['surface_name', 'site_name', 'species_name', 'formation_energy', 'frequencies', 'reference'])

    if verbose:
        print('-------------------------------------------------------------')
        print(f'###### {adsorbate_parameters["desired_surface"]}_{adsorbate_parameters["desired_facet"]}: electric field strength = {field_effects["epsilon"]:.2f} V/A ######')
        print('-------------------------------------------------------------\n')

    if write_gases:
        df_out = write_gas_energies(db_filepath, df_out, reference_gases, dummy_gases, dft_corrections_gases, beef_dft_helmholtz_offset, field_effects, verbose)

    if write_adsorbates:
        df_out = write_adsorbate_energies(db_filepath, df_out, adsorbate_parameters, reference_gases, dft_corrections_gases, field_effects)

    # write corrected energy data to file
    system_dir_path = db_filepath.parent / f'{adsorbate_parameters["desired_surface"]}_{adsorbate_parameters["desired_facet"]}'
    Path.mkdir(system_dir_path, parents=True, exist_ok=True)
    energies_filepath = system_dir_path / f'energies_f{field_effects["epsilon"]:.2e}.txt'
    with open(energies_filepath, 'w') as energies_file:
        df_out.to_string(energies_file, index=False, justify='left')
    return None

def write_gas_energies(db_filepath, df_out, reference_gases, dummy_gases, dft_corrections_gases, beef_dft_helmholtz_offset, field_effects, verbose):
    "Write formation energies to energies.txt after applying free energy corrections"

    # record energies for reference gases
    db = connect(str(db_filepath))
    gas_atoms_rows = list(db.select(state='gas'))
    surface, site, species, formation_energies, frequencies, references = [], [], [], [], [], []
    num_decimal_places = 5
    reference_gas_energies = {}
    for row in gas_atoms_rows:
        if row.formula in dft_corrections_gases:
            reference_gas_energies[row.formula] = row.energy + dft_corrections_gases[row.formula]
        else:
            reference_gas_energies[row.formula] = row.energy

    # build dataframe data for dummy gases
    dummy_gas_energy = 0.0
    for dummy_gas in dummy_gases:
        surface.append('None')
        site.append('gas')
        species.append(dummy_gas)
        formation_energies.append(f'{dummy_gas_energy:.{num_decimal_places}f}')

    # build dataframe data for gaseous species
    for row in gas_atoms_rows:
        surface.append('None')
        site.append('gas')
        if row.formula in reference_gases:
            relative_energy = 0.0
        else:
            chemical_symbols_dict = formula_to_chemical_symbols(row.formula)
            for chemical_symbol in chemical_symbols_dict.keys():
                count = chemical_symbols_dict[chemical_symbol]

            # xCO + (x-z+y/2)H2 --> CxHyOz + (x-z)H2O
            if 'C' in chemical_symbols_dict:
                x = chemical_symbols_dict['C']
            else:
                x = 0
            if 'H' in chemical_symbols_dict:
                y = chemical_symbols_dict['H']
            else:
                y = 0
            if 'O' in chemical_symbols_dict:
                z = chemical_symbols_dict['O']
            else:
                z = 0
            relative_energy = (row.energy
                               + (x - z) * reference_gas_energies['H2O']
                               - x * reference_gas_energies['CO']
                               - (x - z + y / 2) * reference_gas_energies['H2'])

        # Apply BEEF DFT Helmholtz Offset
        if row.formula in beef_dft_helmholtz_offset:
            relative_energy += beef_dft_helmholtz_offset[row.formula]

        # Apply field effects
        epsilon = field_effects['epsilon']
        pH = field_effects['pH']
        U_RHE = field_effects['U_RHE']
        mu = field_effects['mu']
        alpha = field_effects['alpha']

        # U_RHE-scale field effect
#         U_SHE = epsilon * d + UM_PZC
#         U_RHE = U_SHE + 0.059 * pH

        # U_SHE-scale field effects
        if row.formula in mu:
            relative_energy += (mu[row.formula] * epsilon
                                - 0.5 * alpha[row.formula] * epsilon**2)

        species.append(row.formula)
        formation_energies.append(f'{relative_energy:.{num_decimal_places}f}')
        frequencies.append([])
        references.append('')

    df = pd.DataFrame(list(zip(surface, site, species, formation_energies, frequencies, references)),
                       columns=['surface_name', 'site_name', 'species_name', 'formation_energy', 'frequencies', 'reference'])
    df_out = df_out.append(df)

    if verbose:
        table = []
        print('Gas Phase Free Energy Correction:')
        print('---------------------------------')
        for index, species_name in enumerate(df['species_name']):
            beef_correction = dft_corrections_gases[species_name] if species_name in dft_corrections_gases else 0.0
            table.append([species_name, f'{beef_correction:.{num_decimal_places}f}', df["formation_energy"][index]])
        print(tabulate(table, headers=["Species", "E_BEEF (eV)", "E_Formation (eV)"], tablefmt='psql', colalign=("right", "right", "right"), disable_numparse=True))
        print('\n')
    return df_out

def write_adsorbate_energies(db_filepath, df_out, adsorbate_parameters, reference_gases, dft_corrections_gases, field_effects):
    "Write formation energies to energies.txt after applying free energy corrections"

    # identify system ids for adsorbate species
    table_name = 'reaction'
    df1 = db_to_dataframe(table_name, str(db_filepath))

    desired_surface = adsorbate_parameters['desired_surface']
    desired_facet = adsorbate_parameters['desired_facet']
    df1 = df1.loc[df1['surface_composition'] == desired_surface]
    df1 = df1.loc[df1['facet'].str.contains(desired_facet)]
    
    ## build dataframe data for adsorbate species
    db = connect(str(db_filepath))
    surface, site, species, formation_energies, frequencies, references = [], [], [], [], [], []
    num_decimal_places = 9

    # simple reaction species: only one active product and filter out reactions without any adsorbed species
    index_list = []
    for index, product in enumerate(df1['products']):
        if product.count('star') == 1 and 'star' not in json.loads(product):
            index_list.append(index)
    df2 = df1.iloc[index_list]

    products_list = []
    species_list = []
    for index, products_string in enumerate(df2.products):
        products_list.append(json.loads(products_string))
        for product in products_list[-1]:
            if 'star' in product:
                species_list.append(product.replace('star', ''))
    unique_species = sorted(list(set(species_list)), key=len)
    for species_value in unique_species:
        if '-' in desired_surface:
            surface.append(desired_surface.split('-')[0])
        else:
            surface.append(desired_surface)
        site.append(desired_facet)
        species.append(species_value)

        site_wise_formation_energies = get_formation_energies(df2, species_list, species_value, products_list, reference_gases, dft_corrections_gases, adsorbate_parameters, field_effects)
        min_formation_energy = min(site_wise_formation_energies)
        formation_energies.append(f'{min_formation_energy:.{num_decimal_places}f}')
        frequencies.append([])
        references.append('')
    
    df3 = pd.DataFrame(list(zip(surface, site, species, formation_energies, frequencies, references)),
                       columns=['surface_name', 'site_name', 'species_name', 'formation_energy', 'frequencies', 'reference'])
    df_out = df_out.append(df3)
    return df_out

def get_formation_energies(df, species_list, species_value, products_list, reference_gases, dft_corrections_gases, adsorbate_parameters, field_effects):
    "Compute formation energies for a given species at all suitable adsorption sites"
    
    indices = [index for index, value in enumerate(species_list) if value == species_value]
    facet_list = df.facet.iloc[indices].tolist()

    site_wise_formation_energies = []
    for index, reaction_index in enumerate(indices):
        facet = facet_list[index]
        # NOTE: Reactions with unspecified adsorption site in the facet label are constant-charge NEB calculations and irrelevant for formation_energy calculations.
        # Thus, considering only reactions with specified adsorption site in this code.
        if '-' in facet:
            reactants = json.loads(df.reactants.iloc[reaction_index])
            products = products_list[reaction_index]
            reaction_energy = df.reaction_energy.iloc[reaction_index]
            formation_energy = get_adsorbate_formation_energy(species_value, reactants, products, reaction_energy, reference_gases, dft_corrections_gases, adsorbate_parameters, field_effects)
            site_wise_formation_energies.append(formation_energy)
    return site_wise_formation_energies

def get_adsorbate_formation_energy(species_value, reactants, products, reaction_energy, reference_gases, dft_corrections_gases, adsorbate_parameters, field_effects):
    "Compute formation_energy for adsorbate in a given reaction"
    
    product_energy = 0
    for product, num_units in products.items():
        if 'star' not in product:
            if 'gas' in product:
                gas_product = product.replace('gas', '')
                if gas_product not in reference_gases:
                    row_index = df_out.index[df_out['species_name'] == gas_product][0]
                    product_energy += float(df_out['formation_energy'].iloc[row_index]) * num_units
                    
                if gas_product in dft_corrections_gases:
                    product_energy += dft_corrections_gases[gas_product] * num_units

    reactant_energy = 0
    for reactant, num_units in reactants.items():
        if 'star' not in reactant:
            if 'gas' in reactant:
                gas_product = reactant.replace('gas', '')
                if gas_product not in reference_gases:
                    row_index =  df_out.index[df_out['species_name'] == gas_product][0]
                    reactant_energy += float(df_out['formation_energy'].iloc[row_index]) * num_units
        
                if gas_product in dft_corrections_gases:
                    reactant_energy += dft_corrections_gases[gas_product] * num_units

    # Apply solvation energy corrections
    if species_value in adsorbate_parameters['solvation_corrections_adsorbates']:
        formation_energy = reaction_energy + product_energy - reactant_energy + adsorbate_parameters['solvation_corrections_adsorbates'][species_value]
    else:
        formation_energy = reaction_energy + product_energy - reactant_energy

    # Apply field effects
    epsilon = field_effects['epsilon']
    pH = field_effects['pH']
    U_RHE = field_effects['U_RHE']
    mu = field_effects['mu']
    alpha = field_effects['alpha']

    # U_RHE-scale field effect
#     U_SHE = epsilon * d + UM_PZC
#     U_RHE = U_SHE + 0.059 * pH

    # x CO + y (H++e-) = CxH(y-2x+2z)Oz + (x-z) H2O
    # x CO + y/2 H2 = CxH(y-2x+2z)Oz + (x-z) H2O
    # Based on computational hydrogen electrode, n should be twice the number of H2 gas molecules that are required for the reduction reaction
    if 'H2gas' in reactants:
        n = 2 * reactants['H2gas']
    else:
        n = 0
    formation_energy += n * U_RHE

    # U_SHE-scale field effects
    if species_value in mu:
        formation_energy += (mu[species_value] * epsilon
                             - 0.5 * alpha[species_value] * epsilon**2)
    return formation_energy

def formula_to_chemical_symbols(formula):
    "Return dictionary mapping chemical symbols to number of atoms"

    chemical_symbols_dict = {}

    # split chemical formula string into alpha and numeric characters
    regex = re.compile('(\d+|\s+)')
    split_formula = regex.split(formula)
    split_formula_list = []

    # count number of formula units if any
    start_index = 0
    formula_unit_count = 1
    if str.isdigit(split_formula[0]):
        formula_unit_count = int(split_formula[0])
        start_index = 1

    # identify chemical symbol and map to its count
    for string in split_formula[start_index:]:
        if str.isdigit(string):
            chemical_symbols_dict[last_chemical_symbol] = int(string)
        else:
            if len(string) == 0:
                pass
            elif len(string) == 1:
                last_chemical_symbol = string
                chemical_symbols_dict[last_chemical_symbol] = 1
            elif len(string) == 2:
                if string in chemical_symbols:
                    last_chemical_symbol = string
                    chemical_symbols_dict[last_chemical_symbol] = 1
                else:
                    chemical_symbols_dict[string[0]] = 1
                    last_chemical_symbol = string[1]
                    chemical_symbols_dict[last_chemical_symbol] = 1
            elif len(string) == 3:
                if string[0] in chemical_symbols:
                    chemical_symbols_dict[string[0]] = 1
                    last_chemical_symbol = string[1:]
                    chemical_symbols_dict[last_chemical_symbol] = 1
                else:
                    chemical_symbols_dict[string[:2]] = 1
                    last_chemical_symbol = string[2]
                    chemical_symbols_dict[last_chemical_symbol] = 1

    # multiply number of atoms for each chemical symbol with number of formula units
    for key in chemical_symbols_dict.keys():
        chemical_symbols_dict[key] = formula_unit_count * chemical_symbols_dict[key]
    return chemical_symbols_dict
