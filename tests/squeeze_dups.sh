#!/bin/bash

dirTestInp=/tmp/squeeze-test-inp
dirTestOut=/tmp/squeeze-test-out

function create_test_dir()
{
  rm -rf ${dirTestInp} ${dirTestOut}
  mkdir -p ${dirTestInp} && pushd $_ || return 1
  local path1=path-1/to path2=path-2/tooo
  local fileA1=${path1}/a1 fileB1=${path2}/b1 fileA2=${path2}/a2
  mkdir -p ${path1} ${path2}
  echo "A" >> ${fileA1}
  cp ${fileA1} ${fileA2}
  echo "B" >> ${fileB1}
  # tree  # shown after
  popd || return 1
}

# Runs driver.py with the given args and reports whether its exit status matches the expected one.
# Usage: expect_exit STATUS ARGS...   (stdin is passed through, to answer the prompts)
function expect_exit()
{
  local expected=$1; shift
  "${driverPyPath}" "$@"
  local status=$?
  if [[ ${status} -eq ${expected} ]]; then
    echo "OK: exit ${status} for: $*"
  else
    echo "FAIL: exit ${status}, expected ${expected}, for: $*"
    failures=$((failures + 1))
  fi
}

function show_dirs()
{
  echo "INP DIR"
  tree ${dirTestInp}
  echo "OUT DIR"
  tree ${dirTestOut}
}

rootPath="$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")"

pushd "${rootPath}" || exit 1

source ./driver.sh

driverPyPath=./mnemo/driver.py
failures=0

"${driverPyPath}" --help

create_test_dir
# These must fail on the destination checks, before the squeeze mode prompt (stdin closed: any prompt would abort)
expect_exit 1 --squeeze-dups --source ${dirTestInp} --destination ${dirTestInp} </dev/null
expect_exit 1 --squeeze-dups --source ${dirTestInp} --destination ${dirTestInp}/.. </dev/null
expect_exit 1 --squeeze-dups --source ${dirTestInp} --destination ${dirTestInp}/path-1 </dev/null
# --squeeze-mode only makes sense with --squeeze-dups (argparse errors exit with 2)
expect_exit 2 --heavy --squeeze-mode merge --source ${dirTestInp} </dev/null

echo -e "\n\n\n"
create_test_dir
# Answers: y to "Proceed?", then keep copy 1 of the a1/a2 group
expect_exit 0 --squeeze-dups --squeeze-mode discard --source ${dirTestInp} --destination ${dirTestOut} <<< $'y\n1'
show_dirs

echo -e "\n\n\n"
create_test_dir
expect_exit 0 --squeeze-dups --squeeze-mode merge --source ${dirTestInp} --destination ${dirTestOut} <<< $'y\n1'
show_dirs

popd || exit 1

echo -e "\n${failures} failure(s)"
[[ ${failures} -eq 0 ]]
