module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.21.0"

  name = var.name
  cidr = var.cidr

  azs             = var.azs
  public_subnets  = var.public_subnets

  enable_nat_gateway = false
  single_nat_gateway = false
  one_nat_gateway_per_az = false

  enable_dns_hostnames = true
  enable_dns_support   = true

  map_public_ip_on_launch = true

  tags = var.tags
  
   public_subnet_tags = {
    "karpenter.sh/discovery" = var.cluster_name  #Karpenterは「どのSubnetにEC2を起動するか」をタグで検索する。
  }  

}
