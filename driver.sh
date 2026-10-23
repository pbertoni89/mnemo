#!/bin/bash


function mnemo_quick() {
    clear
    "$(dirname "${BASH_SOURCE[0]}")"/mnemo/mnemo.py --whitelist ./whitelist.txt --blacklist ./blacklist.txt --backup
}


function old_stuff_ignore_yet_keep_me() {
    dstPathRoot=/run/media/pbertoni/PBHDL/$(date -I"minutes")-linux-backup
    dstPathArch=${dstPathRoot}.tar.gz
    dstPathLog=${dstPathRoot}.txt

    while true; do
        read -p "Do you wish to backup everything under ${dstPathRoot}? " yn
        case $yn in
            [Yy]* ) break;;
            [Nn]* ) exit;;
            * ) echo "Please answer yes or no.";;
        esac
    done

    set -x

    time sudo tar czfv ${dstPathArch} \
        --exclude=/backup.tar.gz \
        --exclude=/dev \
        --exclude=/mnt \
        --exclude=/proc \
        --exclude=/sys \
        --exclude=/tmp \
        --exclude=/media \
        --exclude=/lost+found \
        --exclude=/home/pbertoni/.cache \
        --exclude=/home/pbertoni/.mozilla \
        --exclude=/run/media/pbertoni/PBHDL \
        --exclude=/run/media/pbertoni/PBHDW \
        / \
            | & tee ${dstPathLog}

    set +x
}
