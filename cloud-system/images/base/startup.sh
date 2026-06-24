#!/bin/bash
# Start SSH daemon
service ssh start

# Keep container running
echo "Cloud instance started at $(date)" > /var/log/cloud-init.log
echo "Hostname: $(hostname)" >> /var/log/cloud-init.log
echo "IP: $(hostname -I)" >> /var/log/cloud-init.log

# Keep alive
tail -f /var/log/cloud-init.log
