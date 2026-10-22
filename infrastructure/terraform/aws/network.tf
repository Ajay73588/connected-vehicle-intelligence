data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name = "${var.name}-${var.environment}"
  cidr = var.vpc_cidr
  azs  = local.azs

  public_subnets   = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, i)]
  private_subnets  = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, i + 10)]
  database_subnets = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, i + 20)]

  enable_nat_gateway           = true
  single_nat_gateway           = var.environment != "prod" # one NAT per AZ in prod for AZ-failure tolerance
  one_nat_gateway_per_az       = var.environment == "prod"
  enable_dns_hostnames         = true
  create_database_subnet_group = true

  enable_flow_log                      = true
  create_flow_log_cloudwatch_log_group = true
  create_flow_log_cloudwatch_iam_role  = true

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

# One security group for the managed data tier; only the EKS nodes may talk to it (no public access anywhere).
resource "aws_security_group" "data" {
  name_prefix = "${var.name}-data-"
  vpc_id      = module.vpc.vpc_id
  description = "Managed data stores (RDS, ElastiCache, MSK, DocumentDB), reachable only from EKS nodes"

  dynamic "ingress" {
    for_each = { postgres = 5432, redis = 6379, kafka-tls = 9094, docdb = 27017 }
    content {
      description     = ingress.key
      from_port       = ingress.value
      to_port         = ingress.value
      protocol        = "tcp"
      security_groups = [module.eks.node_security_group_id]
    }
  }
  lifecycle { create_before_destroy = true }
}
