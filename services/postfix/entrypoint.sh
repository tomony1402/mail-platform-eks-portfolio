#!/bin/bash
set -e

crond

exec /usr/sbin/postfix start-fg
