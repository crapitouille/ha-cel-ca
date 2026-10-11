#!/usr/bin/env bash
# Deploys the storage of a CEL-CA community on Azure (one Storage Account).
# Run from the repository root in Azure Cloud Shell (Bash):
#
#   azure/deploy.sh --community cel-ca --account celcastore --admin-dir ~/clouddrive/cel-admin
#
# Options: --location (switzerlandnorth) --resource-group (rg-<community>) --python (python3)
set -euo pipefail

COMMUNITY="" ACCOUNT="" ADMIN_DIR="" LOCATION="switzerlandnorth" RESOURCE_GROUP="" PYTHON="python3"
while [ $# -gt 0 ]; do
  case "$1" in
    --community) COMMUNITY="$2"; shift 2 ;;
    --account) ACCOUNT="$2"; shift 2 ;;
    --admin-dir) ADMIN_DIR="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --resource-group) RESOURCE_GROUP="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }
step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
py_json() {  # py_json '<python expression on d>' < json
  "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"
}

[[ "$COMMUNITY" =~ ^[a-z0-9][a-z0-9_-]{1,31}$ ]] || die "--community is required (a-z, 0-9, - and _)"
[[ "$ACCOUNT" =~ ^[a-z0-9]{3,24}$ ]] || die "--account is required (3-24 lower-case letters and digits, globally unique)"
[ -n "$ADMIN_DIR" ] || die "--admin-dir is required"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-$COMMUNITY}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ADMIN_TOOL="$ROOT/tools/cel_admin.py"

command -v az >/dev/null || die "Azure CLI (az) is not available"
[ -f "$ADMIN_DIR/cel_registry.json" ] || die "admin directory not initialised: $PYTHON $ADMIN_TOOL --dir $ADMIN_DIR init --community $COMMUNITY"
registry_community="$(py_json 'd["community"]' < "$ADMIN_DIR/cel_registry.json")"
[ "$registry_community" = "$COMMUNITY" ] || die "the admin directory belongs to community '$registry_community'"

step "Checking Azure login"
account_json="$(az account show -o json)"
echo "    subscription: $(py_json 'd["name"]' <<<"$account_json") ($(py_json 'd["id"]' <<<"$account_json"))"

step "Checking storage account name $ACCOUNT"
if ! az storage account show --name "$ACCOUNT" --resource-group "$RESOURCE_GROUP" -o none 2>/dev/null; then
  available="$(az storage account check-name --name "$ACCOUNT" -o json | py_json 'str(d["nameAvailable"]).lower()')"
  [ "$available" = "true" ] || die "storage account name '$ACCOUNT' is already taken - choose another --account"
fi

step "Creating resource group $RESOURCE_GROUP"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --tags application=cel-ca "community=$COMMUNITY" -o none

step "Deploying the storage account"
deployment="$(az deployment group create --resource-group "$RESOURCE_GROUP" --name "cel-storage-$(date +%Y%m%d%H%M%S)" \
  --template-file "$ROOT/azure/main.bicep" \
  --parameters "storageAccountName=$ACCOUNT" "communityId=$COMMUNITY" "location=$LOCATION" -o json)"
TABLE_ENDPOINT="$(py_json 'd["properties"]["outputs"]["tableEndpoint"]["value"]' <<<"$deployment")"
QUEUE_ENDPOINT="$(py_json 'd["properties"]["outputs"]["queueEndpoint"]["value"]' <<<"$deployment")"

step "Configuring the CEL administration"
"$PYTHON" "$ADMIN_TOOL" --dir "$ADMIN_DIR" storage-target --account "$ACCOUNT" --resource-group "$RESOURCE_GROUP" \
  --table-endpoint "$TABLE_ENDPOINT" --queue-endpoint "$QUEUE_ENDPOINT"

step "Self-test (access restrictions of the members)"
"$PYTHON" "$ADMIN_TOOL" --dir "$ADMIN_DIR" selftest

printf '\n\033[32mCEL storage ready: %s\033[0m\n' "$ACCOUNT"
echo "For each HEMS:"
echo "  $PYTHON $ADMIN_TOOL --dir $ADMIN_DIR enroll --pubkey <key> --id <hems-id> --name <name>"
echo "  $PYTHON $ADMIN_TOOL --dir $ADMIN_DIR member-access --id <hems-id>"
echo "  $PYTHON $ADMIN_TOOL --dir $ADMIN_DIR publish"
