output "nightmode_controller_role_arn" {
  value       = aws_iam_role.nightmode_controller.arn
  description = "IAM Role ARN for nightmode-controller IRSA"
}
