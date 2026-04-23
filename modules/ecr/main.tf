resource "aws_ecr_repository" "this" {
  name = var.name

  image_scanning_configuration {
    scan_on_push = true
  }

  image_tag_mutability = "MUTABLE"
  force_delete = true
}
