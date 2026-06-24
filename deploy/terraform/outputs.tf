output "app_url" {
  description = "Public URL once DNS propagates."
  value       = "https://recap.yuchia.dev"
}

output "s3_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "aws_access_key_id" {
  description = "IAM access key id injected into the app Secret (for reference)."
  value       = aws_iam_access_key.recap.id
}

output "aws_secret_access_key" {
  description = "IAM secret key (sensitive)."
  value       = aws_iam_access_key.recap.secret
  sensitive   = true
}

output "next_steps" {
  value = <<-EOT
    1) Build + load the image:   IMAGE_TAG=$(git rev-parse --short HEAD) bash scripts/build_and_load.sh
       then: export TF_VAR_image_tag=$(git rev-parse --short HEAD)  &&  terraform apply
    2) gpud must be running on nlp-gpu-01.be.ucsc.edu (see server/README_deploy.md).
    3) Verify: kubectl -n recap get pods,ingress  &&  open https://recap.yuchia.dev
  EOT
}
