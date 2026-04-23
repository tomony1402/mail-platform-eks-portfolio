# Gateway ノード専用 Security Group
resource "aws_security_group" "gateway_node" {
  name        = "gateway-node-sg"
  description = "Security group for EKS gateway nodes"
  vpc_id      = var.vpc_id

  ingress {
    description = "Allow SMTP from on-prem"
    from_port   = 25
    to_port     = 25
    protocol    = "tcp"
    cidr_blocks = [var.onprem_cidr]
  }

  tags = merge(var.tags, {
    Name = "gateway-node-sg"
  })
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = var.cluster_version

  vpc_id     = var.vpc_id
  subnet_ids = var.subnet_ids

  cluster_endpoint_public_access  = var.endpoint_public_access
  cluster_endpoint_private_access = var.endpoint_private_access

  enable_cluster_creator_admin_permissions = true

  # コントロールプレーンログは全て無効（CloudWatchコスト削減）
  cluster_enabled_log_types = []

  node_security_group_tags = {
    "karpenter.sh/discovery" = var.cluster_name
  }

  cluster_security_group_tags = {
    "karpenter.sh/discovery" = var.cluster_name
  }

  # 配信ノード（共有ノードSG）へ Gateway SG からのポート25インバウンドを追加
  node_security_group_additional_rules = {
    delivery_ingress_smtp_from_gateway = {
      description              = "Allow SMTP port 25 from gateway nodes"
      protocol                 = "tcp"
      from_port                = 25
      to_port                  = 25
      type                     = "ingress"
      source_security_group_id = aws_security_group.gateway_node.id
    }
  }

  eks_managed_node_groups = {
    gateway = {
      name           = "gateway-node"
      instance_types = ["t3.medium"]

      min_size     = 2
      max_size     = 2
      desired_size = 2

      subnet_ids = var.public_subnet_ids

      # Gateway ノード専用 SG をアタッチ
      vpc_security_group_ids = [aws_security_group.gateway_node.id]

      labels = {
        role = "gateway"
      }

      tags = {
        Role = "gateway"
      }
    }
  }
  tags = var.tags
}
