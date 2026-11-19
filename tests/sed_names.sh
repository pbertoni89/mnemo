#!/bin/bash
# Exercises --sed-names on a throwaway tree in /tmp. Exits non-zero on failure.

dirTest=/tmp/sed-names-test

function create_test_dir()
{
  rm -rf ${dirTest}
  mkdir -p ${dirTest}/AbcdPB_Sogno_Kek/inner_PB_Sogno/deep ${dirTest}/other || return 1
  echo 1 > ${dirTest}/AbcdPB_Sogno_Kek/PB_Sogno_1.m4a
  echo 2 > ${dirTest}/AbcdPB_Sogno_Kek/inner_PB_Sogno/deep/PB_Sogno-PB_Sogno.txt
  echo 3 > ${dirTest}/other/pb_sogno_lowercase.txt
  # Conflict: renaming PB_Sogno_x.txt would overwrite the existing sogno_x.txt
  echo 4 > ${dirTest}/other/PB_Sogno_x.txt
  echo 5 > ${dirTest}/other/sogno_x.txt
  # A symlink is renamed itself, its target is left alone
  ln -s ../other ${dirTest}/AbcdPB_Sogno_Kek/link_PB_Sogno
}

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

# Usage: expect_listing DESCRIPTION  (compares the sorted listing of ${dirTest} with stdin)
function expect_listing()
{
  local actual
  actual="$(cd ${dirTest} && find . -mindepth 1 | LC_ALL=C sort)"
  if [[ "${actual}" == "$(cat)" ]]; then
    echo "OK: ${1}"
  else
    echo "FAIL: ${1}. Got:"
    echo "${actual}"
    failures=$((failures + 1))
  fi
}

rootPath="$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")"
pushd "${rootPath}" || exit 1
driverPyPath=./mnemo/driver.py
failures=0

create_test_dir
original="$(cd ${dirTest} && find . -mindepth 1 | LC_ALL=C sort)"

# Bad arguments: argparse errors exit with 2, before anything is scanned
expect_exit 2 --sed-names --source ${dirTest} --from PB_Sogno </dev/null
expect_exit 2 --sed-names --source ${dirTest} --from 'PB Sogno' --to sogno </dev/null
expect_exit 2 --sed-names --source ${dirTest} --from 'PB/Sogno' --to sogno </dev/null
expect_exit 2 --sed-names --source ${dirTest} --from 'PB_Sogno' --to 'so*' </dev/null
expect_exit 2 --sed-names --source ${dirTest} --from PB_Sogno --to '' </dev/null
expect_exit 2 --sed-names --source ${dirTest} --from same --to same </dev/null
expect_exit 2 --heavy --source ${dirTest} --from PB_Sogno --to sogno </dev/null
expect_exit 1 --sed-names --source ${dirTest} ${dirTest}/other --from PB_Sogno --to sogno </dev/null

# Dry run and a "no" answer leave everything as it was
expect_exit 0 --sed-names --source ${dirTest} --from PB_Sogno --to sogno --dry-run </dev/null
expect_exit 1 --sed-names --source ${dirTest} --from PB_Sogno --to sogno <<< n
expect_listing "dry run and 'n' rename nothing" <<< "${original}"

# The real thing
expect_exit 0 --sed-names --source ${dirTest} --from PB_Sogno --to sogno <<< y
expect_listing "names renamed at every depth, conflict and other case left alone" <<'EOF'
./Abcdsogno_Kek
./Abcdsogno_Kek/inner_sogno
./Abcdsogno_Kek/inner_sogno/deep
./Abcdsogno_Kek/inner_sogno/deep/sogno-sogno.txt
./Abcdsogno_Kek/link_sogno
./Abcdsogno_Kek/sogno_1.m4a
./other
./other/PB_Sogno_x.txt
./other/pb_sogno_lowercase.txt
./other/sogno_x.txt
EOF
if [[ "$(readlink ${dirTest}/Abcdsogno_Kek/link_sogno)" == "../other" && "$(cat ${dirTest}/other/sogno_x.txt)" == 5 ]]; then
  echo "OK: symlink target and conflicting file untouched"
else
  echo "FAIL: symlink target or conflicting file changed"
  failures=$((failures + 1))
fi

# Nothing left to rename: still a success
expect_exit 0 --sed-names --source ${dirTest} --from Abcdsogno_Kek_nope --to x </dev/null

rm -rf ${dirTest}
popd || exit 1

echo -e "\n${failures} failure(s)"
[[ ${failures} -eq 0 ]]
