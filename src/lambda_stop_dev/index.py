import os
import logging
from datetime import datetime, timezone, timedelta
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    regiao = os.environ.get('AWS_REGION', 'us-east-2')
    retencao_dias = int(os.environ.get('SNAPSHOT_RETENTION_DAYS', '14'))
    
    ec2 = boto3.client('ec2', region_name=regiao)
    rds = boto3.client('rds', region_name=regiao)
    asg = boto3.client('autoscaling', region_name=regiao)
    
    contadores = {
        'rds': 0,
        'asg': 0,
        'ebs_orfaos': 0,
        'snapshots_expirados': 0,
        'erros': 0
    }
    
    # -------------------------------------------------------------------------
    # 1. PARADA DE BANCOS RDS (DEV)
    # -------------------------------------------------------------------------
    try:
        paginator_rds = rds.get_paginator('describe_db_instances')
        for pagina in paginator_rds.paginate():
            for banco in pagina['DBInstances']:
                tags = {tag['Key'].lower(): tag['Value'].lower() for tag in banco.get('TagList', [])}
                if tags.get('ambiente') == 'dev':
                    identificador = banco['DBInstanceIdentifier']
                    status = banco['DBInstanceStatus']
                    arn = banco['DBInstanceArn']
                    
                    if status == 'available':
                        try:
                            rds.add_tags_to_resource(
                                ResourceName=arn,
                                Tags=[{'Key': 'finops:stopped_by', 'Value': 'finops-scheduler'}]
                            )
                            rds.stop_db_instance(DBInstanceIdentifier=identificador)
                            contadores['rds'] += 1
                            logger.info(f"[RDS] Banco {identificador} parado com sucesso.")
                        except ClientError as e:
                            contadores['erros'] += 1
                            logger.error(f"[RDS] Erro ao parar o banco {identificador}: {e.response['Error']['Message']}")
                    elif status in ['stopped', 'stopping']:
                        logger.info(f"[RDS] Banco {identificador} já se encontra no estado '{status}'. Nenhuma ação necessária.")
                    else:
                        # GAP 2: Estado transitório (ex: backing-up, modifying, storage-optimization, rebooting)
                        contadores['erros'] += 1
                        logger.warning(f"[RDS] Banco {identificador} em estado transitório '{status}'. Não pode ser parado agora; erro registrado para retry assíncrono.")
    except ClientError as e:
        contadores['erros'] += 1
        logger.error(f"[RDS] Falha ao listar bancos de dados: {e.response['Error']['Message']}")

    # -------------------------------------------------------------------------
    # 2. REDUÇÃO DE AUTO SCALING GROUPS (DEV)
    # -------------------------------------------------------------------------
    try:
        paginator_asg = asg.get_paginator('describe_auto_scaling_groups')
        for pagina in paginator_asg.paginate():
            for grupo in pagina['AutoScalingGroups']:
                tags = {tag['Key'].lower(): tag['Value'].lower() for tag in grupo.get('Tags', [])}
                if tags.get('ambiente') == 'dev' and grupo['DesiredCapacity'] > 0:
                    asg_name = grupo['AutoScalingGroupName']
                    prev_desired = str(grupo['DesiredCapacity'])
                    prev_min = str(grupo['MinSize'])
                    try:
                        # Salva o estado real prévio nas tags do ASG para restauração correta
                        asg.create_or_update_tags(Tags=[
                            {
                                'ResourceId': asg_name,
                                'ResourceType': 'auto-scaling-group',
                                'Key': 'finops:previous_desired_capacity',
                                'Value': prev_desired,
                                'PropagateAtLaunch': False
                            },
                            {
                                'ResourceId': asg_name,
                                'ResourceType': 'auto-scaling-group',
                                'Key': 'finops:previous_min_size',
                                'Value': prev_min,
                                'PropagateAtLaunch': False
                            },
                            {
                                'ResourceId': asg_name,
                                'ResourceType': 'auto-scaling-group',
                                'Key': 'finops:stopped_by',
                                'Value': 'finops-scheduler',
                                'PropagateAtLaunch': False
                            }
                        ])
                        
                        asg.update_auto_scaling_group(
                            AutoScalingGroupName=asg_name,
                            MinSize=0,
                            DesiredCapacity=0
                        )
                        contadores['asg'] += 1
                        logger.info(f"[ASG] Grupo {asg_name} zerado (capacidade prévia salva: {prev_desired}, min: {prev_min}).")
                    except ClientError as e:
                        contadores['erros'] += 1
                        logger.error(f"[ASG] Erro ao zerar o grupo {asg_name}: {e.response['Error']['Message']}")
    except ClientError as e:
        contadores['erros'] += 1
        logger.error(f"[ASG] Falha ao listar Auto Scaling Groups: {e.response['Error']['Message']}")

    # -------------------------------------------------------------------------
    # 3. EXPIRAÇÃO E ROTAÇÃO DE SNAPSHOTS EBS LEGADOS (GAP 2: PREVENÇÃO DE VAZAMENTO)
    # -------------------------------------------------------------------------
    limite_expiracao = datetime.now(timezone.utc) - timedelta(days=retencao_dias)
    filtros_snapshots_antigos = [
        {'Name': 'tag:ambiente', 'Values': ['dev']},
        {'Name': 'tag:finops:origin', 'Values': ['auto-cleanup-backup']}
    ]
    try:
        paginator_snapshots = ec2.get_paginator('describe_snapshots')
        for pagina in paginator_snapshots.paginate(OwnerIds=['self'], Filters=filtros_snapshots_antigos):
            for snap in pagina['Snapshots']:
                snap_id = snap['SnapshotId']
                data_criacao = snap['StartTime']
                if data_criacao < limite_expiracao:
                    try:
                        ec2.delete_snapshot(SnapshotId=snap_id)
                        contadores['snapshots_expirados'] += 1
                        logger.info(f"[EBS] Snapshot {snap_id} expirado (> {retencao_dias} dias) excluido com sucesso.")
                    except ClientError as e:
                        contadores['erros'] += 1
                        logger.error(f"[EBS] Falha ao excluir snapshot expirado {snap_id}: {e.response['Error']['Message']}")
    except ClientError as e:
        contadores['erros'] += 1
        logger.error(f"[EBS] Falha ao listar snapshots para rotacao: {e.response['Error']['Message']}")

    # -------------------------------------------------------------------------
    # 4. LIMPEZA SEGURA E IDEMPOTENTE DE VOLUMES EBS ÓRFÃOS (DEV)
    # -------------------------------------------------------------------------
    filtros_ebs = [
        {'Name': 'status', 'Values': ['available']},
        {'Name': 'tag:ambiente', 'Values': ['dev']}
    ]
    try:
        paginator_ebs = ec2.get_paginator('describe_volumes')
        for pagina in paginator_ebs.paginate(Filters=filtros_ebs):
            for volume in pagina['Volumes']:
                vol_id = volume['VolumeId']
                try:
                    # GAP 3: Idempotência - reaproveita snapshot se já existir para este volume
                    filtros_existentes = [
                        {'Name': 'volume-id', 'Values': [vol_id]},
                        {'Name': 'tag:finops:origin', 'Values': ['auto-cleanup-backup']},
                        {'Name': 'status', 'Values': ['pending', 'completed']}
                    ]
                    snaps_existentes = ec2.describe_snapshots(
                        OwnerIds=['self'],
                        Filters=filtros_existentes
                    ).get('Snapshots', [])

                    if snaps_existentes:
                        snap_reutilizado = snaps_existentes[0]['SnapshotId']
                        logger.info(f"[EBS] Snapshot existente {snap_reutilizado} reaproveitado para volume {vol_id} (idempotencia).")
                    else:
                        snapshot = ec2.create_snapshot(
                            VolumeId=vol_id,
                            Description=f"FinOps auto-snapshot de seguranca antes da exclusao do volume {vol_id}",
                            TagSpecifications=[{
                                'ResourceType': 'snapshot',
                                'Tags': [
                                    {'Key': 'Name', 'Value': f"finops-backup-{vol_id}"},
                                    {'Key': 'ambiente', 'Value': 'dev'},
                                    {'Key': 'finops:origin', 'Value': 'auto-cleanup-backup'}
                                ]
                            }]
                        )
                        logger.info(f"[EBS] Snapshot {snapshot['SnapshotId']} criado para o volume {vol_id}.")
                    
                    ec2.delete_volume(VolumeId=vol_id)
                    contadores['ebs_orfaos'] += 1
                    logger.info(f"[EBS] Volume {vol_id} excluido com sucesso.")
                except ClientError as e:
                    contadores['erros'] += 1
                    logger.error(f"[EBS] Erro ao processar o volume {vol_id}: {e.response['Error']['Message']}")
    except ClientError as e:
        contadores['erros'] += 1
        logger.error(f"[EBS] Falha ao listar volumes EBS: {e.response['Error']['Message']}")

    # -------------------------------------------------------------------------
    # 5. CONSOLIDAÇÃO DO RELATÓRIO E PROPAGAÇÃO DE ERROS (GAP 1: DLQ/RETRY)
    # -------------------------------------------------------------------------
    detalhes = []
    if contadores['rds'] > 0:
        detalhes.append(f"{contadores['rds']} RDS parados")
    if contadores['asg'] > 0:
        detalhes.append(f"{contadores['asg']} ASGs zerados")
    if contadores['ebs_orfaos'] > 0:
        detalhes.append(f"{contadores['ebs_orfaos']} Discos deletados (com snapshot)")
    if contadores['snapshots_expirados'] > 0:
        detalhes.append(f"{contadores['snapshots_expirados']} Snapshots legados expirados")
    if contadores['erros'] > 0:
        detalhes.append(f"{contadores['erros']} erros/alertas registrados")
        
    if detalhes:
        relatorio = f"FinOps Executado (STOP)! Status: " + " | ".join(detalhes) + "."
    else:
        relatorio = "FinOps Executado (STOP)! Nenhum recurso precisou ser desligado."
                 
    logger.info(relatorio)

    # Se houver erros, levanta exceção explícita para ativar retries assíncronos do Lambda e encaminhar à DLQ via Invoke Config
    if contadores['erros'] > 0:
        raise RuntimeError(f"Rotina FinOps STOP finalizou com {contadores['erros']} erro(s). Detalhes: {relatorio}")
                 
    return {
        'statusCode': 200,
        'body': relatorio
    }