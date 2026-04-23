variable "oidc_provider_arn" {
  type        = string
  description = "EKS OIDC provider ARN"
}

variable "oidc_provider_url" {
  type        = string
  description = "EKS OIDC provider URL (without https://)"
}

variable "s3_bucket_name" {
  type        = string
  description = "S3 bucket name for nightmode-controller to access"
}
