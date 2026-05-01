variable "oidc_provider_arn" {}
variable "oidc_provider_url" {}
variable "cluster_name" {}
variable "cluster_endpoint" {}
variable "enable_karpenter" {
  type    = bool
  default = false
}

variable "node_role_name" {
  type = string
}
