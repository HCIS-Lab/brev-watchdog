#!/bin/bash
# Authorize the watchdog host's key for the ubuntu user (idempotent).
KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN4Z3m2UKg7RsLG2cQDFfBz9PGEaTfSUui003b55WCUK hcis-s30@hciss30-System-Product-Name'
install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
touch /home/ubuntu/.ssh/authorized_keys
grep -qF "$KEY" /home/ubuntu/.ssh/authorized_keys || echo "$KEY" >> /home/ubuntu/.ssh/authorized_keys
chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys
chmod 600 /home/ubuntu/.ssh/authorized_keys
