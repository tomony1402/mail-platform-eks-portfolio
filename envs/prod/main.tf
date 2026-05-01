module "network" {
  source = "../../modules/network"

  name = var.name
  cidr = var.vpc_cidr
  azs  = var.azs

  public_subnets = var.public_subnets
  cluster_name   = var.cluster_name

  tags = {
    Environment = "prod"
    Project     = var.name
  }
}

module "eks" {
  source = "../../modules/eks"

  cluster_name    = var.cluster_name
  cluster_version = var.cluster_version

  vpc_id            = module.network.vpc_id
  subnet_ids        = module.network.public_subnets
  public_subnet_ids = module.network.public_subnets

  endpoint_public_access  = var.cluster_endpoint_public_access
  endpoint_private_access = var.cluster_endpoint_private_access

  tags = {
    Environment = "prod"
    Project     = var.name
  }
}

module "postfix_ecr" {
  source = "../../modules/ecr"

  name = "postfix"
}


module "vpc_endpoints" {
  source = "../../modules/vpc-endpoints"

  name                   = var.name
  vpc_id                 = module.network.vpc_id
  subnet_ids             = module.network.public_subnets
  route_table_ids        = module.network.public_route_table_ids
  node_security_group_id = module.eks.node_security_group_id
  region                 = "us-east-1"

  tags = {
    Environment = "prod"
    Project     = var.name
  }
}

resource "aws_s3_bucket" "recovery" {
  bucket = "mail-platform-recovery-${var.cluster_name}"

  tags = {
    Environment = "prod"
    Project     = var.name
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "recovery" {
  bucket = aws_s3_bucket.recovery.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "recovery" {
  bucket = aws_s3_bucket.recovery.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "recovery" {
  bucket = aws_s3_bucket.recovery.id

  rule {
    id     = "auto-delete-after-7-days"
    status = "Enabled"

    filter {}

    expiration {
      days = 7
    }
  }
}

module "iam" {
  source = "../../modules/iam"

  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.oidc_provider_url
  s3_bucket_name    = aws_s3_bucket.recovery.bucket
}

module "karpenter" {
  source = "../../modules/karpenter"

  cluster_name     = var.cluster_name
  cluster_endpoint = module.eks.cluster_endpoint

  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.oidc_provider_url

  node_role_name = module.eks.node_group_role_name
}
