terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.60" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
    helm   = { source = "hashicorp/helm", version = "~> 2.14" }
  }
  # Remote state: configure per account (S3 bucket + DynamoDB lock table), e.g.
  # backend "s3" { bucket = "my-tf-state" key = "cvi/prod.tfstate" region = "us-east-1" dynamodb_table = "tf-lock" encrypt = true }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "cvi", Environment = var.environment, ManagedBy = "terraform" } }
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
    }
  }
}
