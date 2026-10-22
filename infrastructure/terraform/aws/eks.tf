module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.24"

  cluster_name    = "${var.name}-${var.environment}"
  cluster_version = var.eks_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  cluster_endpoint_public_access           = true # restrict with cluster_endpoint_public_access_cidrs for production
  enable_cluster_creator_admin_permissions = true
  enable_irsa                              = true # pods assume IAM roles (no static AWS keys in the cluster)

  cluster_enabled_log_types = ["api", "audit", "authenticator"]

  eks_managed_node_groups = {
    default = {
      instance_types = var.node_instance_types
      min_size       = var.node_min
      max_size       = var.node_max
      desired_size   = var.node_min
      disk_size      = 100
      labels         = { workload = "cvi" }
    }
  }
}

# Load balancing: AWS Load Balancer Controller provisions an ALB for Ingress objects; ingress-nginx may be used instead
# (the Kubernetes overlays assume ingress-nginx). External Secrets syncs AWS Secrets Manager into Kubernetes Secrets.
resource "helm_release" "external_secrets" {
  name             = "external-secrets"
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  namespace        = "external-secrets"
  create_namespace = true
  depends_on       = [module.eks]
}

resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  namespace        = "keda"
  create_namespace = true
  depends_on       = [module.eks]
}

resource "helm_release" "ingress_nginx" {
  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  namespace        = "ingress-nginx"
  create_namespace = true
  set {
    name  = "controller.service.annotations.service\\.beta\\.kubernetes\\.io/aws-load-balancer-type"
    value = "nlb"
  }
  depends_on = [module.eks]
}
