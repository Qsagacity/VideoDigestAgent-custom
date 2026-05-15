#!/usr/bin/env bash
set -euo pipefail

cd /home/ubuntu/VideoDigestAgent--Feishu

/usr/bin/flock -n /tmp/category_digest.lock \
  /usr/bin/python3 category_digest.py \
  >> /home/ubuntu/VideoDigestAgent--Feishu/logs/category_digest_cron.log 2>&1
