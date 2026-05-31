#!/bin/bash
terraform apply \
  -var-file=secrets.tfvars \
  -target=adguard_list_filter.hagezi_gambling_tertiary \
  -target=adguard_list_filter.hagezi_threat_intel_tertiary
