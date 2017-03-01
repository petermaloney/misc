#!/bin/bash
#
# generates a db made of only foreign packages that are installed (seen by pacman -Qm) and exist in the cache (/var/cache/pacman/pkg/)
# TODO: also generate from non-installed packages in the cache that are found in AUR but not main repos

outfile=/var/cache/pacman/pkg/peter.db.tar.gz
tmpoutfile=/var/cache/pacman/pkg/tmp.peter.db.tar.gz

IFS=$'\n'
for pkg in $(pacman -Qm); do
    n=$(awk '{print $1}' <<< "$pkg")
    v=$(awk '{print $2}' <<< "$pkg")
    path=$(echo /var/cache/pacman/pkg/"$n"-"$v"*.pkg.tar.xz)
    if [ -e "$path" ]; then
        echo "$path"
    fi
done | xargs repo-add "$tmpoutfile"
mv "$tmpoutfile" "$outfile"

