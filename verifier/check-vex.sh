#!/usr/bin/env bash
# Refuses an OpenVEX document that is broader than the one scanner exception an image is
# allowed. The Trivy step of .github/workflows/verifier-images.yml runs this before it hands
# an image directory's *.openvex.json files to `trivy --vex`, so the workflow that publishes
# an image decides for itself what may be suppressed, whatever another workflow's tests say.
#
#   check-vex.sh RUNTIME [IMAGES_DIR]
#
# Every verifier/images/RUNTIME/*.openvex.json (IMAGES_DIR defaults to the images directory
# next to this script) must be exactly the exception that RUNTIME is allowed, written below:
# one statement, for that one CVE on that one package, not_affected with the justification
# vulnerable_code_not_present, and no other field. A runtime with no allowed exception
# refuses any document. No document at all is fine.
#
# tests/test_check_vex.py pins these rules, and the committed document is one of its cases.

set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 RUNTIME [IMAGES_DIR]" >&2
  exit 2
fi

runtime=$1
images=${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/images}

# The exceptions: runtime, CVE, purl Trivy reports for the package. A statement is only
# ever added here together with its reasoning in verifier/contract.md.
case "$runtime" in
  python)
    allowed_cve=CVE-2025-47273
    allowed_purl=pkg:pypi/setuptools@70.3.0
    ;;
  *)
    allowed_cve=
    allowed_purl=
    ;;
esac

status=0
for document in "$images/$runtime"/*.openvex.json; do
  [ -e "$document" ] || continue

  if [ -z "$allowed_cve" ]; then
    echo "$document: no scanner exception is allowed for the $runtime image" >&2
    status=1
    continue
  fi

  if ! jq -e --arg cve "$allowed_cve" --arg purl "$allowed_purl" '
    ((keys | sort) == ["@context", "@id", "author", "statements", "timestamp", "version"])
    and .["@context"] == "https://openvex.dev/ns/v0.2.0"
    and (.statements | length == 1)
    and (.statements[0] |
      ((keys | sort) == ["impact_statement", "justification", "products", "status", "vulnerability"])
      and .vulnerability == {name: $cve}
      and .products == [{"@id": $purl}]
      and .status == "not_affected"
      and .justification == "vulnerable_code_not_present"
      and (.impact_statement | type == "string" and length > 0))
  ' "$document" >/dev/null 2>&1; then
    echo "$document: not the one exception the $runtime image is allowed ($allowed_cve on $allowed_purl, not_affected, vulnerable_code_not_present, one statement)" >&2
    status=1
  fi
done

exit "$status"
