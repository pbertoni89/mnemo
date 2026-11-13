#!/bin/bash


function mnemo_quick() {
  clear
  "$(dirname "${BASH_SOURCE[0]}")"/mnemo/driver.py --whitelist ./whitelist.txt --blacklist ./blacklist.txt --backup
}


# Usage: mnemo_squeeze_dups DIR [DIR...] [--dry-run] [--destination DEST]
function mnemo_squeeze_dups() {
  "$(dirname "${BASH_SOURCE[0]}")"/mnemo/driver.py --squeeze-dups --source "$@"
}


# Usage: mnemo_stt_setup [force]
# Creates the uv environment (REPO/.venv) that --stt needs: faster-whisper, plus the
# CUDA 12 / cuDNN 9 runtime wheels when an NVIDIA GPU is visible. "force" recreates it.
function mnemo_stt_setup() {
  local repo venv
  repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  venv="${repo}/.venv"

  if ! command -v uv >/dev/null 2>&1; then
    echo "mnemo_stt_setup: uv not found, see https://docs.astral.sh/uv/" >&2
    return 1
  fi

  if [[ "${1}" == "force" ]]; then
    rm -rf "${venv}"
  elif [[ -n "${1}" ]]; then
    echo "Usage: mnemo_stt_setup [force]" >&2
    return 1
  fi

  if [[ ! -x "${venv}/bin/python" ]]; then
    # faster-whisper's native deps lag behind new Python releases, so pin one they support
    uv venv --python 3.12 "${venv}" || return 1
  fi

  # av 19 dropped the metadata_errors argument that faster-whisper 1.2.x passes to av.open()
  local pkgs=(faster-whisper "av<19")
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    echo "mnemo_stt_setup: NVIDIA GPU found, also installing the CUDA runtime libraries"
    pkgs+=(nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*")
  else
    echo "mnemo_stt_setup: no NVIDIA GPU found, transcription will run on CPU"
  fi

  uv pip install --python "${venv}/bin/python" "${pkgs[@]}" || return 1
  "${venv}/bin/python" -c 'import faster_whisper, ctranslate2; print("faster-whisper", faster_whisper.__version__, "- CUDA devices:", ctranslate2.get_cuda_device_count())'
}


# Usage: mnemo_stt DIR [DIR...] [--dry-run]
# Writes FILE.txt next to every .m4a/.aac/.amr FILE under the given dirs.
function mnemo_stt() {
  "$(dirname "${BASH_SOURCE[0]}")"/mnemo/driver.py --stt --source "$@"
}


function mnemo_old_stuff_ignore_yet_keep_me() {
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
      | tee ${dstPathLog}

  set +x
}
