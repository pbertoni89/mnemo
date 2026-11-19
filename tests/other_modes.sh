#!/bin/bash
# Simple checks for the modes without their own test: --backup, --heavy, --crowded, --stt (dry run only,
# as a real run needs the venv and a model), plus source handling. Works in /tmp. Exits non-zero on failure.

dirTest=/tmp/mnemo-other-test
src=${dirTest}/src
dest=${dirTest}/dest
copy=${dest}${src}   # --backup mirrors absolute paths under the destination

# Usage: check DESCRIPTION COMMAND...   (passes if COMMAND succeeds)
function check()
{
  local what=$1; shift
  if "$@"; then
    echo "OK: ${what}"
  else
    echo "FAIL: ${what}"
    failures=$((failures + 1))
  fi
}

# Usage: expect_exit STATUS ARGS...   (runs driver.py; its output is kept in ${out})
function expect_exit()
{
  local expected=$1; shift
  out="$("${driverPyPath}" "$@" 2>&1)"
  local status=$?
  echo "${out}"
  check "exit ${expected} for: $*" test ${status} -eq ${expected}
}

rootPath="$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")"
pushd "${rootPath}" || exit 1
driverPyPath=./mnemo/driver.py
failures=0

rm -rf ${dirTest}
mkdir -p ${src}/many ${src}/repo/.git ${src}/audio ${dest}
head -c 1000000 /dev/zero > ${src}/big.bin
echo small > ${src}/small.txt
echo noise > ${src}/noise.log
for i in 1 2 3 4 5; do echo ${i} > ${src}/many/f${i}; done
echo code > ${src}/repo/code.py
touch ${src}/audio/memo.m4a ${src}/audio/LOUD.AMR ${src}/audio/done.aac ${src}/audio/done.aac.txt
printf '# comments and blank lines are ignored\n\n%s\n' ${src} > ${dirTest}/whitelist.txt
echo "*.log" > ${dirTest}/blacklist.txt

echo "=== sources ==="
expect_exit 1 --heavy --source ${dirTest}/nope
expect_exit 1 --heavy
expect_exit 1 --heavy --whitelist ${dirTest}/nope.txt
expect_exit 2 --heavy --crowded --source ${src}

echo "=== --heavy / --crowded ==="
expect_exit 0 --heavy 1 --whitelist ${dirTest}/whitelist.txt
check "--heavy lists big.bin" grep -q "big.bin" <<< "${out}"
check "--heavy 1 lists nothing else" test "$(grep -c "${src}/" <<< "${out}")" -eq 1
expect_exit 0 --crowded 1 --source ${src}
check "--crowded finds many/" grep -q "^5 ${src}/many$" <<< "${out}"

echo "=== --stt --dry-run ==="
expect_exit 0 --stt --dry-run --source ${src}
check "--stt picks memo.m4a" grep -q "Would write ${src}/audio/memo.m4a.txt" <<< "${out}"
check "--stt picks LOUD.AMR (any case)" grep -q "Would write ${src}/audio/LOUD.AMR.txt" <<< "${out}"
check "--stt skips done.aac (has .txt)" grep -q "Would transcribe 2 files, 1 already have a .txt" <<< "${out}"
check "--stt dry run writes nothing" test ! -e ${src}/audio/memo.m4a.txt

echo "=== --backup ==="
expect_exit 1 --backup --source ${src} --destination ${src}/inside
expect_exit 1 --backup --dry-run --source ${src} --destination /var/tmp/mnemo-on-root
expect_exit 0 --backup --dry-run --no-cache --source ${src} --destination ${dest}
check "dry run copies nothing" test ! -e ${copy}

expect_exit 0 --backup --no-cache --source ${src} --destination ${dest} --blacklist ${dirTest}/blacklist.txt
check "files copied" cmp -s ${src}/big.bin ${copy}/big.bin
check "nested files copied" test -f ${copy}/many/f5
check "git repo excluded by default" test ! -e ${copy}/repo
check "blacklist honoured" test ! -e ${copy}/noise.log

expect_exit 0 --backup --no-cache --also-repos --source ${src} --destination ${dest}
check "--also-repos copies the repo" test -f ${copy}/repo/code.py

rm ${src}/small.txt
expect_exit 1 --backup --no-cache --delete --source ${src} --destination ${dest} <<< n
check "'n' to --delete keeps the file" test -f ${copy}/small.txt
expect_exit 0 --backup --no-cache --delete --source ${src} --destination ${dest} <<< y
check "--delete removes it" test ! -e ${copy}/small.txt

rm -rf ${dirTest}
popd || exit 1

echo -e "\n${failures} failure(s)"
[[ ${failures} -eq 0 ]]
