#!/usr/bin/env python3
#
# Copyright 2015 Peter Maloney
#
# License: Version 2 of the GNU GPL or any later version
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
# 

from os import listdir, walk, symlink, mkdir, makedirs
from os.path import isfile, isdir, exists, join, getsize, dirname, basename
import argparse



parser = argparse.ArgumentParser(description='Splits an input directory into multiple dirs with symlinks to the original files, limiting each group dir to a max_size.')

parser.add_argument('--indir', "-i", action='store',
                   type=str, default=None, required=True,
                   help='input directory')
parser.add_argument('--outdir', "-o", action='store',
                   type=str, default=None, required=True,
                   help='output base directory')
parser.add_argument('--max-size', "-s", action='store',
                   type=int, default=None, required=True,
                   help='Maxiumum size of each created directory, in bytes')

args = parser.parse_args()



in_dir = args.indir
out_base_dir = args.outdir
size_max = args.max_size


if exists(join(out_base_dir,"0")):
    print("ERROR: \"%s\" already exists" % (join(out_base_dir,"0")))
    exit(1)

# first we make a list of the files, so we can modify the list, removing files we already used
allfiles = []

class File:
    def __init__(self, path):
        self.path = path
        self.size = getsize(path)
        
for w in walk(in_dir):
    d=w[0]
    dirs=w[1]
    files=w[2]
    for f in files:
        path = join(d, f)
        fobj = File(path)
        #print("DEBUG: size = %s, path = \"%s\"" % (fobj.size, fobj.path) )
        allfiles += [fobj]
        
        if fobj.size > size_max:
            print("ERROR: file \"%s\" size %s is larger than max %s... cannot complete" % (fobj.path, fobj.size, size_max))
            exit(1)

# Sort by size so the largest file that fits in a group is the one added, so the groups should be as close to the same size as possible
def sort_by_size_desc(fobj):
    return -fobj.size

allfiles = sorted(allfiles, key=sort_by_size_desc)

# Then we'll make a bunch of lists of files that are each lower than size, to prepare to make link dirs
splitgroups = []

print("\nmaking groups")
group_number=0
while len(allfiles) != 0:
    # currently working group here
    group=[]
    group_size=0
    for fobj in allfiles:
        #print("DEBUG: found file: size = %s, path = \"%s\"" % (fobj.size, fobj.path) )
        if( group_size + fobj.size <= size_max ):
            group += [fobj]
            group_size += fobj.size
            
    print("group %s, size %s" % (group_number, group_size))
    for fobj in group:
        print("DEBUG: added file: size = %s, path = \"%s\"" % (fobj.size, fobj.path) )
        allfiles.remove(fobj)

    splitgroups += [group]
    group_number += 1

# Then generate the link dirs, so you can use tar -h to tar them up.
# We make links instead of tars so you don't need to duplicate the data. If you write tars now, and then later to tapes, you wrote tars, plus tapes. If you link now, and then tar and pipe to the tape commands later, you only wrote it once.
#    eg. cd /tmp/split/0
#        tar -h - * | tapecommand
group_number=0
print("\nmaking links")
for group in splitgroups:
    print("group %s" % (group_number))
    for fobj in group:
        relative_dir = dirname(fobj.path[len(in_dir):])
        relative_path = fobj.path[len(in_dir):]
        #print("DEBUG: relative_dir = %s, relative_path = %s" % (relative_dir, relative_path))
        
        outdir = join(out_base_dir, str(group_number), relative_dir)
        outpath = join(out_base_dir, str(group_number), relative_path)
        print("DEBUG: outdir = %s, outpath = %s" % (outdir, outpath))
        
        if not isdir(outdir):
            makedirs(outdir)
        symlink(fobj.path, outpath)
    group_number += 1

print("Done!")