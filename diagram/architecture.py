"""
Network + CI/CD architecture diagram for the Photo Uploader lab.
Generated with the `diagrams` library (diagram-as-code), per the lab's
deliverable requirement. Run with:

    pip install diagrams --break-system-packages   # also needs the Graphviz system package
    python architecture.py

Outputs architecture.png in this directory.
"""

from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import Fargate
from diagrams.aws.network import (
    ALB,
    VPC,
    InternetGateway,
    NATGateway,
    Endpoint,
    CF,
)
from diagrams.aws.devtools import Codepipeline, Codedeploy
from diagrams.aws.storage import S3
from diagrams.aws.database import RDS
from diagrams.aws.management import Cloudwatch
from diagrams.aws.security import IAM, SecretsManager
from diagrams.aws.integration import Eventbridge
from diagrams.onprem.vcs import Github
from diagrams.onprem.ci import GithubActions
from diagrams.generic.place import Datacenter

graph_attr = {
    "fontsize": "20",
    "bgcolor": "white",
    "pad": "0.5",
    "splines": "spline",
}

with Diagram(
    "Photo Uploader Lab",
    filename="architecture",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):

    with Cluster("GitHub"):
        infra_repo = Github("photo-uploader-infra")
        app_repo = Github("photo-uploader-app")
        infra_action = GithubActions("package-templates.yml\n(OIDC, job_workflow_ref-scoped)")
        app_action = GithubActions("build-and-push.yml\n(OIDC, job_workflow_ref-scoped)")
        infra_repo >> infra_action
        app_repo >> app_action

    browser = Datacenter("Browser\n(no auth)")

    with Cluster("AWS Account / Region"):

        iam_oidc = IAM("GitHub OIDC\nprovider + 2 scoped roles")

        templates_bucket = S3("CFN templates\nbucket")
        infra_action >> Edge(label="upload nested\ntemplates (OIDC)") >> templates_bucket

        cdn = CF("CloudFront\n(Price Class 200, OAC)")
        photos_bucket = S3("Photos bucket\n(private, OAC-only)")
        cdn >> Edge(label="signed origin\nfetch (OAC)") >> photos_bucket
        browser >> Edge(label="GET image\n(HTTPS)") >> cdn

        with Cluster("VPC (multi-AZ, 10.30.0.0/16)"):
            igw = InternetGateway("Internet\nGateway")

            with Cluster("Public Subnets (AZ-1 / AZ-2)"):
                nat = NATGateway("NAT GW")
                alb = ALB("Application\nLoad Balancer")

            with Cluster("Private Subnets (AZ-1 / AZ-2)"):
                with Cluster("ECS Cluster (Fargate, 1-4 tasks, CPU target-tracking)"):
                    svc_blue = Fargate("Task(s)\nBLUE")
                    svc_green = Fargate("Task(s)\nGREEN")

                with Cluster("Interface VPC Endpoints"):
                    ecr_ep = Endpoint("ecr.api / ecr.dkr")
                    logs_ep = Endpoint("logs")
                    sts_ep = Endpoint("sts")
                    secrets_ep = Endpoint("secretsmanager")

                db = RDS("PostgreSQL\n(db.t3.micro)\nphoto metadata")

            igw >> alb
            browser >> Edge(label="upload / view gallery\n(HTTP :80)") >> alb
            alb >> Edge(label="prod listener :80") >> svc_blue
            alb >> Edge(label="traffic shifts here\non deploy", style="dashed") >> svc_green
            svc_blue >> Edge(label="JDBC :5432") >> db
            svc_green >> Edge(label="JDBC :5432") >> db
            svc_blue >> ecr_ep
            svc_green >> ecr_ep
            [svc_blue, svc_green] >> secrets_ep >> SecretsManager("RDS-managed\nDB credential")

        svc_blue >> Edge(label="PutObject (task role)") >> photos_bucket
        svc_green >> Edge(label="PutObject (task role)") >> photos_bucket

        ecr_repo = S3("ECR repo\n(app image)")
        ecr_ep >> ecr_repo
        logs_ep >> Cloudwatch("CloudWatch\nLogs")

        eventbridge = Eventbridge("EventBridge rule\n(ECR PUSH :latest)")
        pipeline = Codepipeline("CodePipeline")
        codedeploy = Codedeploy("CodeDeploy\n(blue/green)")

        app_action >> Edge(label="docker push\n:sha-xxx + :latest (OIDC)") >> ecr_repo
        ecr_repo >> Edge(label="PutImage event") >> eventbridge
        eventbridge >> Edge(label="StartPipelineExecution") >> pipeline
        app_repo >> Edge(label="source: appspec.yaml\n+ taskdef.json\n(DetectChanges: false)", style="dotted") >> pipeline
        pipeline >> codedeploy
        codedeploy >> Edge(label="register new task def,\nshift ALB traffic") >> svc_green

        templates_bucket >> Edge(label="Git sync deploys\nroot.yaml", style="bold") >> Datacenter("CloudFormation\nnested stacks")
