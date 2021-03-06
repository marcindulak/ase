"""This module defines an ASE interface to VASP.

The path of the directory containing the pseudopotential
directories (potpaw,potpaw_GGA, potpaw_PBE, ...) should be set
by the environmental flag $VASP_PP_PATH.

The user should also set one of the following environmental flags, which
instructs ASE on how to execute VASP: $ASE_VASP_COMMAND, $VASP_COMMAND, or
$VASP_SCRIPT.

The user can set the environmental flag $VASP_COMMAND pointing
to the command use the launch vasp e.g. 'vasp_std' or 'mpirun -n 16 vasp_std'

Alternatively, the user can also set the environmental flag
$VASP_SCRIPT pointing to a python script looking something like::

   import os
   exitcode = os.system('vasp_std')

www.vasp.at
"""
from __future__ import print_function, division

import os
import sys
import numpy as np
import subprocess
from contextlib import contextmanager
from warnings import warn

import ase
from ase.io import read
from ase.utils import basestring

from ase.calculators.calculator import (FileIOCalculator, ReadError,
                                        all_changes,
                                        PropertyNotImplementedError)

from .create_input import GenerateVaspInput


class Vasp2(GenerateVaspInput, FileIOCalculator):
    """ASE interface for the Vienna Ab initio Simulation Package (VASP),
    with the FileIOCalculator interface.

        Parameters:

            atoms:  object
                Attach an atoms object to the calculator.

            label: str
                Prefix for the output file, and sets the working directory.
                Default is 'vasp'.

            directory: str
                Set the working directory. Is prepended to ``label``.

            restart: str or bool
                Sets a label for the directory to load files from.
                if :code:`restart=True`, the working directory from
                ``label`` is used.

            txt: bool, None, str or writable object
                - If txt is None, default ouput stream will be to PREFIX.out,\
                    where PREFIX is determined by ``label``, i.e. the default\
                    would be vasp.out.

                - If txt is False or '-' the output will be sent through stdout

                - If txt is a string a file will be opened,\
                    and the output will be sent to that file.

                - Finally, txt can also be a an output stream,\
                    which has a 'write' attribute.

                - Example:

                    >>> Vasp2(label='mylabel', txt=None) # Redirect stdout to :file:`mylabel.out`
                    >>> Vasp2(txt='myfile.txt') # Redirect stdout to :file:`myfile.txt`
                    >>> Vasp2(txt='-') # Print vasp output to stdout

            command: str
                Custom instructions on how to execute VASP. Has priority over
                environment variables.
    """
    name = 'Vasp2'

    # Environment commands
    env_commands = ['ASE_VASP_COMMAND', 'VASP_COMMAND', 'VASP_SCRIPT']

    implemented_properties = ['energy', 'free_energy', 'forces', 'dipole',
                              'fermi', 'stress', 'magmom', 'magmoms']

    default_parameters = {}     # Can be used later to set some ASE defaults

    def __init__(self,
                 atoms=None,
                 restart=None,
                 directory='',
                 label='vasp',
                 ignore_bad_restart_file=False,
                 command=None,
                 txt=None,
                 **kwargs):

        # Initialize parameter dictionaries
        GenerateVaspInput.__init__(self)
        self._store_param_state()  # Initialize an empty parameter state

        # Store atoms objects from vasprun.xml here, when an index is read
        # Format: self.xml_data[index] = atoms_object
        self.xml_data = {}

        label = os.path.join(directory, label)

        if restart is True:
            # We restart in the label directory
            restart = label

        FileIOCalculator.__init__(self, restart, ignore_bad_restart_file,
                                  label, atoms, command, **kwargs)

        # Overwrite the command from the FileIOCalculator init
        # as we might have other options than ASE_VASP_COMMAND
        # Also forces the user to have VASP installed first,
        # avoids issues with trying to e.g. get POTCAR's if not installed
        # XXX: Do we want to initialize this later?
        self.command = self.make_command(command)

        self.set_txt(txt)       # Set the output txt stream

        # XXX: This seems to break restarting, unless we return first.
        # Do we really still need to enfore this?

        #  # If no XC combination, GGA functional or POTCAR type is specified,
        #  # default to PW91. This is mostly chosen for backwards compatiblity.
        # if kwargs.get('xc', None):
        #     pass
        # elif not (kwargs.get('gga', None) or kwargs.get('pp', None)):
        #     self.input_params.update({'xc': 'PW91'})
        # # A null value of xc is permitted; custom recipes can be
        # # used by explicitly setting the pseudopotential set and
        # # INCAR keys
        # else:
        #     self.input_params.update({'xc': None})

    def make_command(self, command=None):
        """Return command if one is passed, otherwise try to find
        ASE_VASP_COMMAND, VASP_COMMAND or VASP_SCRIPT.
        If none are set, a RuntimeError is raised"""
        if command:
            cmd = command
        else:
            # Search for the environment commands
            for env in self.env_commands:
                if env in os.environ:
                        cmd = os.environ[env].replace('PREFIX', self.prefix)
                        if env == 'VASP_SCRIPT':
                            # Make the system python exe run $VASP_SCRIPT
                            exe = sys.executable
                            cmd = ' '.join([exe, cmd])
                        break
            else:
                msg = ('Please set either command in calculator'
                       ' or one of the following environment'
                       'variables (prioritized as follows): {}').format(
                           ', '.join(self.env_commands))
                raise RuntimeError(msg)
        return cmd

    def set(self, **kwargs):
        """Override the set function, to test for changes in the
        Vasp FileIO Calculator, then call the create_input.set()
        on remaining inputs for VASP specific keys.

        Allows for setting ``label``, ``directory`` and ``txt``
        without resetting the results in the calculator.
        """
        changed_parameters = {}

        if 'label' in kwargs:
            label = kwargs.pop('label')
            self.set_label(label)

        if 'directory' in kwargs:
            # If we explicitly set directory, overwrite the one in label.
            # XXX: Should we just raise an error here if clash?
            directory = kwargs.pop('directory')
            label = os.path.join(directory, self.prefix)
            self.set_label(label)

        if 'txt' in kwargs:
            txt = kwargs.pop('txt')
            self.set_txt(txt)

        if 'atoms' in kwargs:
            atoms = kwargs.pop('atoms')
            self.set_atoms(atoms)  # Resets results

        changed_parameters.update(FileIOCalculator.set(self, **kwargs))

        # We might at some point add more to changed parameters, or use it
        if changed_parameters:
            self.results.clear()   # We don't want to clear atoms

        if kwargs:
            # If we make any changes to Vasp input, we always reset
            GenerateVaspInput.set(self, **kwargs)
            self.results.clear()

    @contextmanager
    def txt_outstream(self):
        """Custom function for opening a text output stream. Uses self.txt to determine
        the output stream, and accepts a string or an open writable object.
        If a string is used, a new stream is opened, and automatically closes
        the new stream again when exiting.

        Examples:
        # Pass a string
        calc.set_txt('vasp.out')
        with calc.txt_outstream() as out:
            calc.run(out=out)   # Redirects the stdout to 'vasp.out'

        # Use an existing stream
        mystream = open('vasp.out', 'w')
        calc.set_txt(mystream)
        with calc.txt_outstream() as out:
            calc.run(out=out)
        mystream.close()

        # Print to stdout
        calc.set_txt(False)
        with calc.txt_outstream() as out:
            calc.run(out=out)   # output is written to stdout
        """

        opened = False          # Track if we opened a file
        out = None              # Default
        if self.txt:
            if isinstance(self.txt, basestring):
                out = open(self.txt, 'w')
                opened = True
            elif hasattr(self.txt, 'write'):
                out = self.txt
            else:
                raise RuntimeError('txt should either be a string'
                                   'or an I/O stream, got {}'.format(
                                       self.txt))

        try:
            yield out
        finally:
            if opened:
                out.close()

    def calculate(self, atoms=None, properties=['energy'],
                  system_changes=all_changes):
        """Do a VASP calculation in the specified directory.

        This will generate the necessary VASP input files, and then
        execute VASP. After execution, the energy, forces. etc. are read
        from the VASP output files.
        """

        if atoms is not None:
            self.atoms = atoms.copy()

        self.check_cell()      # Check for zero-length lattice vectors
        self.xml_data = {}     # Reset the stored data

        self.write_input(self.atoms, properties, system_changes)

        olddir = os.getcwd()
        try:
            os.chdir(self.directory)

            # Create the text output stream and run VASP
            with self.txt_outstream() as out:
                errorcode = self.run(command=self.command, out=out)
        finally:
            os.chdir(olddir)

        if errorcode:
            raise RuntimeError('{} in {} returned an error: {:d}'.format(
                               self.name, self.directory, errorcode))

        # Read results from calculation
        self.update_atoms(atoms)
        self.read_results()

    def run(self, command=None, out=None):
        """Method to explicitly execute VASP"""
        if command is None:
            command = self.command
        errorcode = subprocess.call(command, shell=True, stdout=out)
        return errorcode

    def check_state(self, atoms, tol=1e-15):
        """Check for system changes since last calculation."""

        def compare_dict(d1, d2):
            """Helper function to compare dictionaries"""
            # Use symmetric difference to find keys which aren't shared
            # for python 2.7 compatiblity
            if set(d1.keys()) ^ set(d2.keys()):
                return False

            # Check for differences in values
            for key, value in d1.items():
                if np.any(value != d2[key]):
                    return False
            return True

        # First we check for default changes
        system_changes = FileIOCalculator.check_state(self, atoms, tol=tol)

        # We now check if we have made any changes to the input parameters
        # XXX: Should we add these parameters to all_changes?
        for param_string, old_dict in self.param_state.items():
            param_dict = getattr(self, param_string)  # Get current param dict
            if not compare_dict(param_dict, old_dict):
                system_changes.append(param_string)

        return system_changes

    def _store_param_state(self):
        """Store current parameter state"""
        self.param_state = dict(
            float_params=self.float_params.copy(),
            exp_params=self.exp_params.copy(),
            string_params=self.string_params.copy(),
            int_params=self.int_params.copy(),
            input_params=self.input_params.copy(),
            bool_params=self.bool_params.copy(),
            list_int_params=self.list_int_params.copy(),
            list_bool_params=self.list_bool_params.copy(),
            list_float_params=self.list_float_params.copy(),
            dict_params=self.dict_params.copy())

    def write_input(self, atoms, properties=['energies'],
                    system_changes=all_changes):
        """Write VASP inputfiles, INCAR, KPOINTS and POTCAR"""
        # Create the folders where we write the files, if we aren't in the
        # current working directory.
        FileIOCalculator.write_input(self, atoms, properties, system_changes)

        self.initialize(atoms)

        GenerateVaspInput.write_input(self, atoms, directory=self.directory)

    def read(self, label=None):
        """Read results from VASP output files.
        Files which are read: OUTCAR, CONTCAR and vasprun.xml
        Raises ReadError if they are not found"""
        if label is None:
            label = self.label
        FileIOCalculator.read(self, label)

        # If we restart, self.parameters isn't initialized
        if self.parameters is None:
            self.parameters = self.get_default_parameters()

        # Check for existence of the necessary output files
        for file in ['OUTCAR', 'CONTCAR', 'vasprun.xml']:
            filename = os.path.join(self.directory, file)
            if not os.path.isfile(filename):
                raise ReadError(
                    'VASP outputfile {} was not found'.format(filename))

        # Read atoms
        self.atoms = self.read_atoms()

        # Build sorting and resorting lists
        self.read_sort()

        # Read parameters
        olddir = os.getcwd()
        try:
            os.chdir(self.directory)
            self.read_incar()
            self.read_kpoints()
            self.read_potcar()
        finally:
            os.chdir(olddir)

        # Read the results from the calculation
        self.read_results()

    def read_sort(self):
        """Create the sorting and resorting list from ase-sort.dat.
        If the ase-sort.dat file does not exist, the sorting is redone.
        """
        sortfile = os.path.join(self.directory, 'ase-sort.dat')
        if os.path.isfile(sortfile):
            self.sort = []
            self.resort = []
            with open(sortfile, 'r') as f:
                for line in f:
                    sort, resort = line.split()
                    self.sort.append(int(sort))
                    self.resort.append(int(resort))
        else:
            # Redo the sorting
            self.initialize(self.atoms)

    def read_atoms(self, filename='CONTCAR'):
        """Read the atoms from file located in the VASP
        working directory. Defaults to CONTCAR."""
        filename = os.path.join(self.directory, filename)
        return read(filename)

    def update_atoms(self, atoms):
        """Update the atoms object with new positions and cell"""
        atoms_sorted = read(os.path.join(self.directory, 'CONTCAR'))
        if (self.int_params['ibrion'] is not None and
                self.int_params['nsw'] is not None):
            if self.int_params['ibrion'] > -1 and self.int_params['nsw'] > 0:
                # Update atomic positions and unit cell with the ones read
                # from CONTCAR.
                atoms.positions = atoms_sorted[self.resort].positions
                atoms.cell = atoms_sorted.cell

        self.atoms = atoms[self.sort].copy()

    def check_cell(self, atoms=None):
        """Check if there is a zero unit cell"""
        if not atoms:
            atoms = self.atoms
        if not atoms.cell.any():
            raise ValueError("The lattice vectors are zero! "
                             "This is the default value - please specify a "
                             "unit cell.")

    def read_results(self):
        """Read the results from VASP output files"""
        # Temporarily load OUTCAR into memory
        outcar = self.load_file('OUTCAR')

        # First we check convergence
        self.converged = self.read_convergence(lines=outcar)

        # Read the data we can from vasprun.xml
        atoms_xml = self.read_from_xml()
        xml_data = {
            'free_energy': atoms_xml.get_potential_energy(
                force_consistent=True),
            'energy': atoms_xml.get_potential_energy(),
            'forces': atoms_xml.get_forces()[self.resort],
            'stress': self.read_stress_xml(),
            'fermi': atoms_xml.calc.get_fermi_level()}
        self.results.update(xml_data)

        # Parse the outcar, as some properties are not loaded in vasprun.xml
        # This is typically pretty fastA
        self.read_outcar(lines=outcar)

        # Update results dict with results from OUTCAR
        # which aren't written to the atoms object we read from
        # the vasprun.xml file.
        # XXX: Should be fixed in the XML reader!
        self.results['magmom'] = self.magnetic_moment
        self.results['magmoms'] = self.magnetic_moments
        # self.results['fermi'] = self.fermi
        self.results['dipole'] = self.dipole

        # Store the parameters used for this calculation
        self._store_param_state()

    # Below defines some functions for faster access to certain common keywords
    @property
    def kpts(self):
        """Access the kpts from input_params dict"""
        return self.input_params['kpts']

    @kpts.setter
    def kpts(self, kpts):
        """Set kpts in input_params dict"""
        self.input_params['kpts'] = kpts

    @property
    def encut(self):
        """Direct access to the encut parameter"""
        return self.float_params['encut']

    @encut.setter
    def encut(self, encut):
        """Direct access for setting the encut parameter"""
        self.set(encut=encut)

    @property
    def xc(self):
        """Direct access to the xc parameter"""
        return self.get_xc_functional()

    @xc.setter
    def xc(self, xc):
        """Direct access for setting the xc parameter"""
        self.set(xc=xc)

    def set_atoms(self, atoms):
        self.atoms = atoms.copy()
        self.results.clear()

    # Below defines methods for reading output files
    def load_file(self, filename):
        """Reads a file in the directory, and returns the lines

        Example:
        >>> outcar = load_file('OUTCAR')
        """
        filename = os.path.join(self.directory, filename)
        with open(filename, 'r') as f:
            return f.readlines()

    def read_outcar(self, lines=None):
        """Read results from the OUTCAR file"""
        if not lines:
            lines = self.load_file('OUTCAR')
        # Spin polarized calculation?
        self.spinpol = self.get_spin_polarized()

        self.version = self.get_version()

        # XXX: Do we want to read all of this again?
        self.energy_free, self.energy_zero = self.read_energy(lines=lines)
        self.forces = self.read_forces(lines=lines)
        self.fermi = self.read_fermi(lines=lines)

        self.dipole = self.read_dipole(lines=lines)

        self.stress = self.read_stress(lines=lines)
        self.nbands = self.read_nbands(lines=lines)

        self.read_ldau()
        p = self.int_params
        q = self.list_float_params
        if self.spinpol:
            self.magnetic_moment = self.read_magnetic_moment()
            if ((p['lorbit'] is not None and p['lorbit'] >= 10) or
                (p['lorbit'] is None and q['rwigs'])):
                self.magnetic_moments = self.read_magnetic_moments(lines=lines)
            else:
                warn(('Magnetic moment data not written in OUTCAR (LORBIT<10),'
                      ' setting magnetic_moments to zero.\nSet LORBIT>=10'
                      ' to get information on magnetic moments'))
                self.magnetic_moments = np.zeros(len(self.atoms))
        else:
            self.magnetic_moment = 0.0
            self.magnetic_moments = np.zeros(len(self.atoms))

    def read_from_xml(self, index=-1, filename='vasprun.xml', overwrite=False):
        """Read vasprun.xml, and return an atoms object at a given index.
        If we have not read the index before, we will read the xml file
        at the given index and store it, before returning

        Parameters:

        filename: str
            Filename of the .xml file. Default value: 'vasprun.xml'
        overwrite: bool
            Force overwrite the existing data in xml_data
            Default value: False
        index: int
            Default returns the last configuration, index=-1
        """
        if overwrite or index not in self.xml_data:
            self.xml_data[index] = read(os.path.join(self.directory,
                                                     filename),
                                        index=index)
        return self.xml_data[index]

    def read_stress_xml(self, index=-1):
        """Read stress tensor from the vasprun.xml file.
        Returns None if there is no stress tensor in the calculation.

        Use get_stress() instead of accessing this method.
        """
        atoms = self.read_from_xml(index)
        try:
            return atoms.get_stress()
        except PropertyNotImplementedError:
            # The tensor was not loaded in the XML file
            return None

    def get_ibz_k_points(self, index=-1):
        atoms = self.read_from_xml(index)
        return atoms.calc.ibz_kpts

    def get_kpt(self, kpt=0, spin=0, index=-1):
        atoms = self.read_from_xml(index)
        return atoms.calc.get_kpt(kpt=kpt, spin=spin)

    def get_eigenvalues(self, kpt=0, spin=0, index=-1):
        atoms = self.read_from_xml(index)
        return atoms.calc.get_eigenvalues(kpt=kpt, spin=spin)

    def get_fermi_level(self, index=-1):
        atoms = self.read_from_xml(index)
        return atoms.calc.get_fermi_level()

    def get_homo_lumo(self, index=-1):
        atoms = self.read_from_xml(index)
        return atoms.calc.get_homo_lumo()

    def get_homo_lumo_by_spin(self, spin=0, index=-1):
        atoms = self.read_from_xml(index)
        return atoms.calc.get_homo_lumo_by_spin(spin=spin)

    def get_occupation_numbers(self, kpt=0, spin=0, index=-1):
        atoms = self.read_from_xml(index)
        return atoms.calc.get_occupation_numbers(kpt, spin)

    def get_spin_polarized(self, index=-1):
        atoms = self.read_from_xml(index)
        return atoms.calc.get_spin_polarized()

    def get_number_of_spins(self, index=-1):
        atoms = self.read_from_xml(index)
        return atoms.calc.get_number_of_spins()

    def get_number_of_bands(self):
        return self.nbands

    def get_number_of_electrons(self, lines=None):
        if not lines:
            lines = self.load_file('OUTCAR')

        nelect = None
        for line in lines:
            if 'total number of electrons' in line:
                nelect = float(line.split('=')[1].split()[0].strip())
                break
        return nelect

    def get_version(self):
        """Get the VASP version number"""
        # The version number is the first occurence, so we can just
        # load the OUTCAR, as we will return soon anyway
        filename = os.path.join(self.directory, 'OUTCAR')
        with open(filename, 'r') as f:
            for line in f:
                if ' vasp.' in line:
                    return line[len(' vasp.'):].split()[0]
            else:
                # We didn't find the verison in VASP
                return None

    def get_number_of_iterations(self):
        return self.read_number_of_iterations()

    def read_number_of_iterations(self, lines=None):
        if not lines:
            lines = self.load_file('OUTCAR')
        niter = None
        for line in lines:
            # find the last iteration number
            if '- Iteration' in line:
                niter = int(line.split(')')[0].split('(')[-1].strip())
        return niter

    def read_stress(self, lines=None):
        """Read stress from OUTCAR.

        Depreciated: Use get_stress() instead.
        """
        # We don't really need this, as we read this from vasprun.xml
        # keeping it around "just in case" for now
        if not lines:
            lines = self.load_file('OUTCAR')

        stress = None
        for line in lines:
            if ' in kB  ' in line:
                stress = -np.array([float(a) for a in line.split()[2:]])
                stress = stress[[0, 1, 2, 4, 5, 3]] * 1e-1 * ase.units.GPa
        return stress

    def read_ldau(self, lines=None):
        """Read the LDA+U values from OUTCAR"""
        if not lines:
            lines = self.load_file('OUTCAR')

        ldau_luj = None
        ldauprint = None
        ldau = None
        ldautype = None
        atomtypes = []
        # read ldau parameters from outcar
        for line in lines:
            if line.find('TITEL') != -1:    # What atoms are present
                atomtypes.append(
                    line.split()[3].split('_')[0].split('.')[0])
            if line.find('LDAUTYPE') != -1:  # Is this a DFT+U calculation
                ldautype = int(line.split('=')[-1])
                ldau = True
                ldau_luj = {}
            if line.find('LDAUL') != -1:
                L = line.split('=')[-1].split()
            if line.find('LDAUU') != -1:
                U = line.split('=')[-1].split()
            if line.find('LDAUJ') != -1:
                J = line.split('=')[-1].split()
        # create dictionary
        if ldau:
            for i, symbol in enumerate(atomtypes):
                ldau_luj[symbol] = {'L': int(L[i]),
                                    'U': float(U[i]),
                                    'J': float(J[i])}
            self.dict_params['ldau_luj'] = ldau_luj

        self.ldau = ldau
        self.ldauprint = ldauprint
        self.ldautype = ldautype
        self.ldau_luj = ldau_luj
        return ldau, ldauprint, ldautype, ldau_luj

    def get_xc_functional(self):
        """Returns the XC functional or the pseudopotential type

        If a XC recipe is set explicitly with 'xc', this is returned.
        Otherwise, the XC functional associated with the
        pseudopotentials (LDA, PW91 or PBE) is returned.
        The string is always cast to uppercase for consistency
        in checks."""
        if self.input_params.get('xc', None):
            return self.input_params['xc'].upper()
        elif self.input_params.get('pp', None):
            return self.input_params['pp'].upper()
        else:
            raise ValueError('No xc or pp found.')

    # Methods for reading information from OUTCAR files:
    def read_energy(self, all=None, lines=None):
        """Method to read energy from OUTCAR file.
        Depreciated: use get_potential_energy() instead"""
        if not lines:
            lines = self.load_file('OUTCAR')

        [energy_free, energy_zero] = [0, 0]
        if all:
            energy_free = []
            energy_zero = []
        for line in lines:
            # Free energy
            if line.lower().startswith('  free  energy   toten'):
                if all:
                    energy_free.append(float(line.split()[-2]))
                else:
                    energy_free = float(line.split()[-2])
            # Extrapolated zero point energy
            if line.startswith('  energy  without entropy'):
                if all:
                    energy_zero.append(float(line.split()[-1]))
                else:
                    energy_zero = float(line.split()[-1])
        return [energy_free, energy_zero]

    def read_forces(self, all=False, lines=None):
        """Method that reads forces from OUTCAR file.

        If 'all' is switched on, the forces for all ionic steps
        in the OUTCAR file be returned, in other case only the
        forces for the last ionic configuration is returned."""

        if not lines:
            lines = self.load_file('OUTCAR')

        if all:
            all_forces = []

        for n, line in enumerate(lines):
            if 'TOTAL-FORCE' in line:
                forces = []
                for i in range(len(self.atoms)):
                    forces.append(np.array([float(f) for f in
                                            lines[n + 2 + i].split()[3:6]]))

                if all:
                    all_forces.append(np.array(forces)[self.resort])

        if all:
            return np.array(all_forces)
        else:
            return np.array(forces)[self.resort]

    def read_fermi(self, lines=None):
        """Method that reads Fermi energy from OUTCAR file"""
        if not lines:
            lines = self.load_file('OUTCAR')

        E_f = None
        for line in lines:
            if 'E-fermi' in line:
                E_f = float(line.split()[2])
        return E_f

    def read_dipole(self, lines=None):
        """Read dipole from OUTCAR"""
        if not lines:
            lines = self.load_file('OUTCAR')

        dipolemoment = np.zeros([1, 3])
        for line in lines:
            if 'dipolmoment' in line:
                dipolemoment = np.array([float(f) for
                                         f in line.split()[1:4]])
        return dipolemoment

    def read_magnetic_moments(self, lines=None):
        """Read magnetic moments from OUTCAR"""
        if not lines:
            lines = self.load_file('OUTCAR')

        magnetic_moments = np.zeros(len(self.atoms))

        for n, line in enumerate(lines):
            if line.rfind('magnetization (x)') > -1:
                for m in range(len(self.atoms)):
                    magnetic_moments[m] = float(lines[n + m + 4].split()[4])
        return np.array(magnetic_moments)[self.resort]

    def read_magnetic_moment(self, lines=None):
        """Read magnetic moment from OUTCAR"""
        if not lines:
            lines = self.load_file('OUTCAR')

        for n, line in enumerate(lines):
            if 'number of electron  ' in line:
                magnetic_moment = float(line.split()[-1])
        return magnetic_moment

    def read_nbands(self, lines=None):
        """Read number of bands from OUTCAR"""
        if not lines:
            lines = self.load_file('OUTCAR')

        for line in lines:
            line = self.strip_warnings(line)
            if 'NBANDS' in line:
                return int(line.split()[-1])

    def read_convergence(self, lines=None):
        """Method that checks whether a calculation has converged."""
        if not lines:
            lines = self.load_file('OUTCAR')

        converged = None
        # First check electronic convergence
        for line in lines:
            if 0:  # vasp always prints that!
                if line.rfind('aborting loop') > -1:  # scf failed
                    raise RuntimeError(line.strip())
                    break
            if 'EDIFF  ' in line:
                ediff = float(line.split()[2])
            if 'total energy-change' in line:
                # I saw this in an atomic oxygen calculation. it
                # breaks this code, so I am checking for it here.
                if 'MIXING' in line:
                    continue
                split = line.split(':')
                a = float(split[1].split('(')[0])
                b = split[1].split('(')[1][0:-2]
                # sometimes this line looks like (second number wrong format!):
                # energy-change (2. order) :-0.2141803E-08  ( 0.2737684-111)
                # we are checking still the first number so
                # let's "fix" the format for the second one
                if 'e' not in b.lower():
                    # replace last occurrence of - (assumed exponent) with -e
                    bsplit = b.split('-')
                    bsplit[-1] = 'e' + bsplit[-1]
                    b = '-'.join(bsplit).replace('-e', 'e-')
                b = float(b)
                if [abs(a), abs(b)] < [ediff, ediff]:
                    converged = True
                else:
                    converged = False
                    continue
        # Then if ibrion in [1,2,3] check whether ionic relaxation
        # condition been fulfilled
        if ((self.int_params['ibrion'] in [1, 2, 3] and
             self.int_params['nsw'] not in [0])):
            if not self.read_relaxed():
                converged = False
            else:
                converged = True
        return converged

    def read_k_point_weights(self, filename='IBZKPT'):
        """Read k-point weighting. Defaults to IBZKPT file."""

        lines = self.load_file(filename)

        if 'Tetrahedra\n' in lines:
            N = lines.index('Tetrahedra\n')
        else:
            N = len(lines)
        kpt_weights = []
        for n in range(3, N):
            kpt_weights.append(float(lines[n].split()[3]))
        kpt_weights = np.array(kpt_weights)
        kpt_weights /= np.sum(kpt_weights)

        return kpt_weights

    def read_relaxed(self, lines=None):
        """Check if ionic relaxation completed"""
        if not lines:
            lines = self.load_file('OUTCAR')
        for line in lines:
            if 'reached required accuracy' in line:
                return True
        return False

    def read_spinpol(self, lines=None):
        """Method which reads if a calculation from spinpolarized using OUTCAR.

        Depreciated: Use get_spin_polarized() instead.
        """
        if not lines:
            lines = self.load_file('OUTCAR')

        for line in lines:
            if 'ISPIN' in line:
                if int(line.split()[2]) == 2:
                    self.spinpol = True
                else:
                    self.spinpol = False
        return self.spinpol

    def strip_warnings(self, line):
        """Returns empty string instead of line from warnings in OUTCAR."""
        if line[0] == "|":
            return ""
        else:
            return line

    def set_txt(self, txt):
        if txt is None:
            # Default behavoir, write to vasp.out
            self.txt = self.prefix + '.out'
        elif txt == '-' or txt is False:
            # We let the output be sent through stdout
            # Do we ever want to completely suppress output?
            self.txt = False
        else:
            self.txt = txt

    def get_number_of_grid_points(self):
        raise NotImplementedError

    def get_pseudo_density(self):
        raise NotImplementedError

    def get_pseudo_wavefunction(self, n=0, k=0, s=0, pad=True):
        raise NotImplementedError

    def get_bz_k_points(self):
        raise NotImplementedError

    def read_vib_freq(self, lines=None):
        """Read vibrational frequencies.

        Returns list of real and list of imaginary frequencies."""
        freq = []
        i_freq = []

        if not lines:
            lines = self.load_file('OUTCAR')

        for line in lines:
            data = line.split()
            if 'THz' in data:
                if 'f/i=' not in data:
                    freq.append(float(data[-2]))
                else:
                    i_freq.append(float(data[-2]))
        return freq, i_freq

    def get_nonselfconsistent_energies(self, bee_type):
        """ Method that reads and returns BEE energy contributions
            written in OUTCAR file.
        """
        assert bee_type == 'beefvdw'
        cmd = 'grep -32 "BEEF xc energy contributions" OUTCAR | tail -32'
        p = os.popen(cmd, 'r')
        s = p.readlines()
        p.close()
        xc = np.array([])
        for i, l in enumerate(s):
            l_ = float(l.split(":")[-1])
            xc = np.append(xc, l_)
        assert len(xc) == 32
        return xc
