#!/usr/bin/env bash
# Write ColdVault's IAM policy documents for your account and buckets.
#
#   ./make-policies.sh ACCOUNT_ID BUCKET [BUCKET …]
#
# Creates out/*.json next to this script. Makes NO AWS calls — read them,
# then apply them with the commands in README.md. Names can be changed with
# MASTER_USER=…, AGENT_USER=…, DELETE_ROLE=… in the environment, and
# NO_MFA=1 writes a delete-role trust policy that doesn't require MFA
# (not recommended).

set -euo pipefail

if [ $# -lt 2 ]; then
  sed -n '2,/^$/s/^# \{0,1\}//p' "$0"
  exit 2
fi
acct=$1; shift
[[ $acct =~ ^[0-9]{12}$ ]] || { echo "ACCOUNT_ID must be the 12-digit AWS account number" >&2; exit 2; }
for b in "$@"; do
  [[ $b =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || { echo "not a valid bucket name: $b" >&2; exit 2; }
done

MASTER_USER=${MASTER_USER:-coldvault-master}
AGENT_USER=${AGENT_USER:-coldvault-agent}
DELETE_ROLE=${DELETE_ROLE:-ColdVaultDelete}
out="$(cd "$(dirname "$0")" && pwd)/out"
mkdir -p "$out"

buckets="" objects=""
for b in "$@"; do
  buckets+="${buckets:+, }\"arn:aws:s3:::$b\""
  objects+="${objects:+, }\"arn:aws:s3:::$b/*\""
done

# The everyday user on the master: upload, restore, download, list, audit —
# and unlock the delete role. The explicit Deny means it can never delete by
# itself, even if a broader policy (e.g. AmazonS3FullAccess) is attached later.
cat > "$out/master-user.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "ListMyBuckets", "Effect": "Allow",
     "Action": "s3:ListAllMyBuckets", "Resource": "*"},
    {"Sid": "Buckets", "Effect": "Allow",
     "Action": ["s3:ListBucket", "s3:GetBucketVersioning"],
     "Resource": [$buckets]},
    {"Sid": "Objects", "Effect": "Allow",
     "Action": ["s3:PutObject", "s3:GetObject", "s3:RestoreObject", "s3:AbortMultipartUpload"],
     "Resource": [$objects]},
    {"Sid": "UnlockDeleteRole", "Effect": "Allow",
     "Action": "sts:AssumeRole",
     "Resource": "arn:aws:iam::$acct:role/$DELETE_ROLE"},
    {"Sid": "NeverDeleteAsThisUser", "Effect": "Deny",
     "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
     "Resource": [$objects]}
  ]
}
EOF

# A second machine (agent): upload its drives, download restores. No listing,
# no restores (the master does those), never deletes.
cat > "$out/agent-user.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "Objects", "Effect": "Allow",
     "Action": ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"],
     "Resource": [$objects]},
    {"Sid": "NeverDelete", "Effect": "Deny",
     "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
     "Resource": [$objects]}
  ]
}
EOF

# The delete role: only deleting, nothing else.
cat > "$out/delete-role.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "DeleteArchivedObjects", "Effect": "Allow",
     "Action": "s3:DeleteObject",
     "Resource": [$objects]}
  ]
}
EOF

# Who may unlock it: only the master's everyday user, and only with a fresh
# MFA code (NO_MFA=1 drops that condition).
if [ "${NO_MFA:-}" = "1" ]; then
  condition=""
else
  condition=',
     "Condition": {"Bool": {"aws:MultiFactorAuthPresent": "true"},
                   "NumericLessThan": {"aws:MultiFactorAuthAge": "300"}}'
fi
cat > "$out/delete-role-trust.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Principal": {"AWS": "arn:aws:iam::$acct:user/$MASTER_USER"},
     "Action": "sts:AssumeRole"$condition}
  ]
}
EOF

echo "wrote $out:"
ls -1 "$out"
echo
echo "master user: $MASTER_USER · agent user: $AGENT_USER · delete role: $DELETE_ROLE"
echo "buckets: $*"
echo "Next: follow deploy/iam/README.md (needs an AWS administrator profile)."
