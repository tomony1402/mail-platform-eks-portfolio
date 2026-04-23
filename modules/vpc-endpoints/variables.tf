variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type        = list(string)
  description = "Subnet IDs for Interface endpoints (us-east-1a, us-east-1b)"
}

variable "route_table_ids" {
  type        = list(string)
  description = "Route table IDs for S3 Gateway endpoint"
}

variable "node_security_group_id" {
  type        = string
  description = "EKS node security group ID to allow HTTPS access to endpoints"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "tags" {
  type    = map(string)
  default = {}
}
