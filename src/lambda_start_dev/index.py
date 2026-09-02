import os
import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    regiao = os.environ.get('AWS_REGION', 'us-east-2')
    rds = boto3.client('rds', region_name=regiao)
    asg = boto3.client('autoscaling', region_name=regiao)
    
    contadores = {'rds': 0, 'asg': 0, 'erros': 0}
    
    # -------------------------------------------------------------------------
    # 1. INICIALIZAÇÃO DE BANCOS RDS (DEV PARADOS PELO FINOPS)
    # -------------------------------------------------------------------------
    try:
        paginator_rds = rds.get_paginator('describe_db_instances')
        for pagina in paginator_rds.paginate():
            for banco in pagina['DBInstances']:
                tags = {tag['Key'].lower(): tag['Value'].lower() for tag in banco.get('TagList', [])}
                identificador = banco['DBInstanceIdentifier']
                arn = banco['DBInstanceArn']
                status = banco['DBInstanceStatus']
                
                # Só religa se for dev E tiver sido desligado pela automação FinOps
                if tags.get('ambiente') == 'dev' and tags.get('finops:stopped_by') == 'finops-scheduler':
                    if status == 'stopped':
                        try:
                            rds.start_db_instance(DBInstanceIdentifier=identificador)
                            
                            rds.remove_tags_from_resource(
                                ResourceName=arn,
                                TagKeys=['finops:stopped_by']
                            )
                            contadores['rds'] += 1
                            logger.info(f"[RDS] Banco {identificador} iniciado com sucesso e tag de controle removida.")
                        except ClientError as e:
                            contadores['erros'] += 1
                            logger.error(f"[RDS] Erro ao iniciar o banco {identificador}: {e.response['Error']['Message']}")
                    elif status in ['starting', 'available']:
                        # GAP 01: Se o banco já estiver ativo/iniciando mas ainda possuir a tag finops:stopped_by (ex: falha de retry anterior),
                        # remove a tag residual para garantir idempotência e evitar religamentos espúrios no futuro.
                        try:
                            rds.remove_tags_from_resource(
                                ResourceName=arn,
                                TagKeys=['finops:stopped_by']
                            )
                            logger.info(f"[RDS] Banco {identificador} já em status '{status}'. Tag residual de controle removida com sucesso (idempotência).")
                        except ClientError as e:
                            contadores['erros'] += 1
                            logger.error(f"[RDS] Erro ao remover tag residual do banco {identificador}: {e.response['Error']['Message']}")
                    else:
                        contadores['erros'] += 1
                        logger.warning(f"[RDS] Banco {identificador} em estado inesperado '{status}'. Erro registrado para acionar retry assíncrono.")
    except ClientError as e:
        contadores['erros'] += 1
        logger.error(f"[RDS] Falha ao listar bancos de dados: {e.response['Error']['Message']}")

    # -------------------------------------------------------------------------
    # 2. RESTAURAÇÃO DE AUTO SCALING GROUPS (DEV PARADOS PELO FINOPS)
    # -------------------------------------------------------------------------
    try:
        paginator_asg = asg.get_paginator('describe_auto_scaling_groups')
        for pagina in paginator_asg.paginate():
            for grupo in pagina['AutoScalingGroups']:
                tags = {tag['Key'].lower(): tag['Value'].lower() for tag in grupo.get('Tags', [])}
                asg_name = grupo['AutoScalingGroupName']
                
                # Só restaura se for dev, estiver zerado E tiver sido desligado pela automação
                if (tags.get('ambiente') == 'dev' and 
                    grupo['DesiredCapacity'] == 0 and 
                    tags.get('finops:stopped_by') == 'finops-scheduler'):
                    
                    try:
                        # Recupera a capacidade original exata salva nas tags (com fallback de segurança para 1)
                        target_desired = int(tags.get('finops:previous_desired_capacity', 1))
                        target_min = int(tags.get('finops:previous_min_size', 1))
                        
                        asg.update_auto_scaling_group(
                            AutoScalingGroupName=asg_name,
                            MinSize=target_min,
                            DesiredCapacity=target_desired
                        )
                        
                        asg.delete_tags(Tags=[
                            {
                                'ResourceId': asg_name,
                                'ResourceType': 'auto-scaling-group',
                                'Key': 'finops:previous_desired_capacity'
                            },
                            {
                                'ResourceId': asg_name,
                                'ResourceType': 'auto-scaling-group',
                                'Key': 'finops:previous_min_size'
                            },
                            {
                                'ResourceId': asg_name,
                                'ResourceType': 'auto-scaling-group',
                                'Key': 'finops:stopped_by'
                            }
                        ])
                        
                        contadores['asg'] += 1
                        logger.info(f"[ASG] Grupo {asg_name} restaurado para capacidade original: {target_desired} (min: {target_min}).")
                    except (ClientError, ValueError) as e:
                        contadores['erros'] += 1
                        logger.error(f"[ASG] Erro ao restaurar o grupo {asg_name}: {str(e)}")
                # GAP 01: Caso o ASG já tenha sido restaurado (capacidade > 0) em tentativa anterior mas as tags residuais ainda existam
                elif (tags.get('ambiente') == 'dev' and 
                      grupo['DesiredCapacity'] > 0 and 
                      tags.get('finops:stopped_by') == 'finops-scheduler'):
                    try:
                        asg.delete_tags(Tags=[
                            {
                                'ResourceId': asg_name,
                                'ResourceType': 'auto-scaling-group',
                                'Key': 'finops:previous_desired_capacity'
                            },
                            {
                                'ResourceId': asg_name,
                                'ResourceType': 'auto-scaling-group',
                                'Key': 'finops:previous_min_size'
                            },
                            {
                                'ResourceId': asg_name,
                                'ResourceType': 'auto-scaling-group',
                                'Key': 'finops:stopped_by'
                            }
                        ])
                        logger.info(f"[ASG] Grupo {asg_name} já se encontra ativo (capacidade: {grupo['DesiredCapacity']}). Tags residuais removidas com sucesso (idempotência).")
                    except ClientError as e:
                        contadores['erros'] += 1
                        logger.error(f"[ASG] Erro ao remover tags residuais do grupo {asg_name}: {str(e)}")
    except ClientError as e:
        contadores['erros'] += 1
        logger.error(f"[ASG] Falha ao listar Auto Scaling Groups: {e.response['Error']['Message']}")

    # -------------------------------------------------------------------------
    # 3. CONSOLIDAÇÃO DO RELATÓRIO E PROPAGAÇÃO DE ERROS (GAP 1: DLQ/RETRY)
    # -------------------------------------------------------------------------
    detalhes = []
    if contadores['rds'] > 0:
        detalhes.append(f"{contadores['rds']} RDS ligados")
    if contadores['asg'] > 0:
        detalhes.append(f"{contadores['asg']} ASGs restaurados")
    if contadores['erros'] > 0:
        detalhes.append(f"{contadores['erros']} erros/alertas registrados")
        
    if detalhes:
        relatorio = f"Ambiente INICIADO (START)! Status: " + " | ".join(detalhes) + "."
    else:
        relatorio = "Ambiente INICIADO (START)! Nenhum recurso precisou ser alterado."
                 
    logger.info(relatorio)

    # Se houver erros, levanta exceção explícita para ativar retries assíncronos do Lambda e encaminhar à DLQ via Invoke Config
    if contadores['erros'] > 0:
        raise RuntimeError(f"Rotina FinOps START finalizou com {contadores['erros']} erro(s). Detalhes: {relatorio}")
                 
    return {
        'statusCode': 200,
        'body': relatorio
    }