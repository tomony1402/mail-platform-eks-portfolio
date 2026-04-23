resource "aws_iam_role" "nightmode_controller" {
  name = "nightmode-controller-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = var.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${var.oidc_provider_url}:sub" = [
              "system:serviceaccount:kube-system:nightmode-controller",
              "system:serviceaccount:kube-system:s3-recovery",
            ]
          }
        }
      }
    ]
  })
}

resource "aws_iam_policy" "nightmode_controller_s3" {
  name = "nightmode-controller-s3-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "NightmodeS3Access"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject"
        ]
        Resource = "arn:aws:s3:::${var.s3_bucket_name}/*"
      },
      {
        Sid    = "NightmodeS3ListBucket"
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = "arn:aws:s3:::${var.s3_bucket_name}"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "nightmode_controller_s3" {
  role       = aws_iam_role.nightmode_controller.name
  policy_arn = aws_iam_policy.nightmode_controller_s3.arn
}
