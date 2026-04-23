output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_security_group_id" {
  value = module.eks.cluster_security_group_id
}

output "oidc_provider_arn" {
  value = module.eks.oidc_provider_arn
}

output "oidc_provider_url" {
  value = replace(module.eks.oidc_provider, "https://", "")
}

output "cluster_certificate_authority_data" {
  value = module.eks.cluster_certificate_authority_data
}


output "node_group_role_name" {
  value = module.eks.eks_managed_node_groups["gateway"].iam_role_name
}

output "node_security_group_id" {
  value = module.eks.node_security_group_id
}
