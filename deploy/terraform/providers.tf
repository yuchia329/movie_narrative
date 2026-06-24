terraform {
  required_version = ">= 1.5"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
    kustomization = {
      source  = "kbst/kustomization"
      version = "~> 0.9"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
  }
}

# k3s API. The server cert isn't valid for the public IP, so the simplest path is an SSH
# local-forward to the API (see README): `ssh -L 6443:localhost:6443 hubstream`, then a
# kubeconfig whose server is https://127.0.0.1:6443 with the k3s CA/client certs.
provider "kubernetes" {
  config_path = pathexpand(var.kubeconfig_path) # expand ~ reliably (CI/non-shell contexts)
}

provider "kustomization" {
  kubeconfig_path = pathexpand(var.kubeconfig_path)
}

# AWS credentials come from the environment (AWS_PROFILE / AWS_ACCESS_KEY_ID …) — never
# hardcode them here.
provider "aws" {
  region = var.aws_region
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
