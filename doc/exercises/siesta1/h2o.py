from ase import *
import numpy as npy

# Set up a H2O molecule by specifying atomic positions
h2o = Atoms(symbols='H2O',
            positions=[( 0.776070, 0.590459, 0.00000),
                       (-0.776070, 0.590459, 0.00000),
                       (0.000000,  -0.007702,  -0.000001)],
            pbc=(1,1,1))

# Center the molecule in the cell with some vacuum around
h2o.center(vacuum=6.)

# Select some energy-shifts for the basis orbitals
e_shifts = [0.01,0.1,0.2,0.3,0.4,0.5]

# Run the relaxation for each energy shift, and print out the
# corresponding total energy
for e_s in e_shifts:
    calc = Siesta('h2o',meshcutoff=200.0*Ry,mix=0.5,pulay=4)
    calc.set_fdf('PAO.EnergyShift', e_s * eV)
    calc.set_fdf('PAO.SplitNorm', 0.15)
    calc.set_fdf('PAO.BasisSize', 'SZ')
    calc.set_fdf('DM.UseSaveDM', 'Y')
    h2o.set_calculator(calc)
    dyn = QuasiNewton(h2o, trajectory='h2o-%s.traj' % e_s)
    dyn.run(fmax=0.02)
    E = h2o.get_potential_energy()
    print "E_shift  Energy"       # Print E_shifts and total energy      
    print "%.2f %.4f" % (e_s,E)
    d = h2o.get_distance(0,2)
    print "E_shift  Bond length"  # Print E_shifts and bond length      
    print "%.2f %.4f" % (e_s,d)
    p = h2o.positions
    d1 = p[0]-p[2]
    d2 = p[1]-p[2]
    r = npy.dot(d1,d2) / (npy.linalg.norm(d1)*npy.linalg.norm(d2))
    angle = npy.arccos(r) / npy.pi * 180
    print "E_shift  Bond angle"   # Print E_shifts and bond angle      
    print "%.2f %.4f" % (e_s,angle)
