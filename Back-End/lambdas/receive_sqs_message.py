# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import json
import os
import uuid
import decimal
import copy
from ast import literal_eval
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Attr, Key


# Helper class for Dynamo
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):  # pylint: disable=E0202
        if isinstance(obj, decimal.Decimal):
            return int(obj)
        return super(DecimalEncoder, self).default(obj)


# Try grab OS environment details
try:
    source_account = os.environ['ENV_SOURCE_ACCOUNT']
    source_region = os.environ['ENV_SOURCE_REGION']
    cross_account_role = os.environ['ENV_CROSS_ACCOUNT_ROLE']
    table_name_multi = os.environ['ENV_TABLE_NAME_MULTI']
    queue_url = os.environ['ENV_SQSQUEUE']
except Exception as e:
    print(f'Error: No os.environment in lambda....: {e}')


# Try connect Clients
try:
    client_sqs = boto3.client('sqs', region_name=source_region)
    dynamodb = boto3.resource('dynamodb', region_name=source_region)
    table = dynamodb.Table(table_name_multi)
except Exception as e:
    print(f'Error: failed to speak to dynamo or sqs....: {e}')


# event = {
#     'queryStringParameters': {
#         'function': 'cron'
#     }
# }


# Assume Role for sub accounts
def assume_sts_role(account_to_assume, cross_account_role_name):

    # https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_switch-role-api.html
    sts_client = boto3.client('sts')
    cross_account_role_arn = f'arn:aws:iam::{account_to_assume}:role/{cross_account_role_name}'

    try:

        # Call the assume_role method of the STSConnection object and pass the role
        # ARN and a role session name.
        credentials = sts_client.assume_role(
            RoleArn=cross_account_role_arn,
            RoleSessionName='TemporaryRole'

        )['Credentials']

        # Make temp creds
        temporary_credentials = boto3.Session(
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
        )

        # return creds
        return temporary_credentials

    except ClientError as e:
        print(
            f'Error: on Account: {account_to_assume} with Role: {cross_account_role_arn}')
        print(f'{cross_account_role_name} might not exists in account?')
        raise e


# Create Boto Client
def create_boto_client(account_number, region, service, cross_account_role):

    # Use boto3 on source account
    if account_number == source_account:
        client = boto3.client(service, region)
        print(f'skipping STS for local account: {account_number}')

    else:
        # Log into Accounts with STS
        assume_creds = assume_sts_role(account_number, cross_account_role)
        client = assume_creds.client(service, region)
        print(f'Logged into Account: {account_number}')

    return client


# Get Lambda Functions
def get_all_lambda(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Change boto client
    client_lambda = create_boto_client(
        account_number, region, 'lambda', cross_account_role)

    # Page all ec2
    paginator = client_lambda.get_paginator('list_functions')

    for page in paginator.paginate():
        for i in page['Functions']:

            # clean role name out of arn
            iam_role = str(i['Role']).split(':')[5].split('/')[1]

            var_list.append(
                {
                    'EntryType': 'lambda',
                    'Region': str(region),
                    'FunctionName': str(i['FunctionName']),
                    'FunctionArn': str(i['FunctionArn']),
                    'Runtime': str(i['Runtime']),
                    'AccountNumber': str(account_number),
                    'Timeout': str(i['Timeout']),
                    'RoleName': str(iam_role),
                    'MemorySize': str(i['MemorySize']),
                    'LastModified': str(i['LastModified'])
                })

    return var_list


# Get RDS Function
def get_all_rds(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Change boto client
    client_rds = create_boto_client(
        account_number, region, 'rds', cross_account_role)

    # Page all db instances
    paginator = client_rds.get_paginator('describe_db_instances')

    for page in paginator.paginate():
        for i in page['DBInstances']:
            var_list.append(
                {
                    'EntryType': 'rds',
                    'Region': str(region),
                    'AccountNumber': str(account_number),
                    'State': str(i['DBInstanceStatus']),
                    'DBInstanceIdentifier': i['DBInstanceIdentifier'],
                    'DBInstanceClass': i['DBInstanceClass'],
                    'Engine': i['Engine'],
                    'MultiAZ': i['MultiAZ'],
                    'PubliclyAccessible': i['PubliclyAccessible']
                })

    return var_list


# Get EC2 Function
def get_all_ec2(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_ec2 = create_boto_client(
        account_number, region, 'ec2', cross_account_role)

    # Page all ec2
    paginator = client_ec2.get_paginator('describe_instances')

    for page in paginator.paginate():
        for i in page['Reservations']:

            # Check for IAM Role
            checkIAMrole = i['Instances'][0].get('IamInstanceProfile', ' ')

            # Convert from string to dict if not empty
            if checkIAMrole != ' ':
                python_dict = literal_eval(f'{checkIAMrole}')
                full_role_name = python_dict['Arn']

                # clean role name out of arn
                iam_role = full_role_name.split(':')[5].split('/')[1]
            else:
                iam_role = ' '

            # Get vCPU count
            vcpu_core = i['Instances'][0]['CpuOptions']['CoreCount']
            vcpu_thread = i['Instances'][0]['CpuOptions']['ThreadsPerCore']

            # Cores x thread = vCPU count
            vCPU = int(vcpu_core) * int(vcpu_thread)

            var_list.append(
                {
                    'EntryType': 'ec2',
                    'InstanceId': i['Instances'][0]['InstanceId'],
                    'State': i['Instances'][0]['State']['Name'],
                    'AccountNumber': str(account_number),
                    'Region': str(region),
                    'vCPU': int(vCPU),
                    'KeyName': i['Instances'][0].get('KeyName', ' '),
                    'RoleName': str(iam_role),
                    'PrivateIpAddress': i['Instances'][0].get('PrivateIpAddress', ' '),
                    'PublicIpAddress': i['Instances'][0].get('PublicIpAddress', ' '),
                    'InstancePlatform': i['Instances'][0].get('Platform', 'Linux/UNIX'),
                    'InstanceType': i['Instances'][0]['InstanceType']
                })

    return var_list


# Get IAM Roles Function
def get_all_iam_roles(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_iam = create_boto_client(
        account_number, region, 'iam', cross_account_role)

    # Page roles
    paginator = client_iam.get_paginator('list_roles')

    for page in paginator.paginate():
        for i in page['Roles']:
            var_list.append(
                {
                    'Arn': str(i['Arn']),
                    'EntryType': 'iam-roles',
                    'Region': 'us-east-1',
                    'AccountNumber': str(account_number),
                    'RoleName': i['RoleName'],
                    'CreateDate': str(i['CreateDate'])
                })

    return var_list


# Get IAM Users Function
def get_all_iam_users(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_iam = create_boto_client(
        account_number, region, 'iam', cross_account_role)

    # Page users
    paginator = client_iam.get_paginator('list_users')

    for page in paginator.paginate():
        for i in page['Users']:
            var_list.append(
                {
                    'Arn': str(i['Arn']),
                    'EntryType': 'iam-users',
                    'AccountNumber': str(account_number),
                    'Region': 'us-east-1',
                    'UserName': str(i['UserName']),
                    'PasswordLastUsed': str(i.get('PasswordLastUsed', ' ')),
                    'CreateDate': str(i['CreateDate'])
                })

    return var_list


# Get IAM Users Function
def get_all_iam_attached_policys(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_iam = create_boto_client(
        account_number, region, 'iam', cross_account_role)

    # Page policys
    paginator = client_iam.get_paginator('list_policies')

    for page in paginator.paginate(OnlyAttached=True):
        for i in page['Policies']:
            var_list.append(
                {
                    'Arn': str(i['Arn']),
                    'EntryType': 'iam-attached-policys',
                    'AccountNumber': str(account_number),
                    'Region': 'us-east-1',
                    'PolicyName': str(i['PolicyName']),
                    'AttachmentCount': int(i['AttachmentCount'])
                })

    return var_list


# Get OnDemand Capacity Reservations Function
def get_all_odcr(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_ec2 = create_boto_client(
        account_number, region, 'ec2', cross_account_role)

    # Page all reservations
    paginator = client_ec2.get_paginator('describe_capacity_reservations')

    for page in paginator.paginate():
        for i in page['CapacityReservations']:
            if i['State'] == 'active':
                var_list.append(
                    {
                        'EntryType': 'odcr',
                        'AccountNumber': str(account_number),
                        'Region': str(region),
                        'AvailabilityZone': i['AvailabilityZone'],
                        'AvailableInstanceCount': i['AvailableInstanceCount'],
                        'CapacityReservationId': i['CapacityReservationId'],
                        'Qty Available': f"{i['AvailableInstanceCount']} of {i['TotalInstanceCount']}",
                        'CreateDate': str(i['CreateDate']),
                        'EbsOptimized': i['EbsOptimized'],
                        'EndDateType': str(i['EndDateType']),
                        'EphemeralStorage': i['EphemeralStorage'],
                        'InstanceMatchCriteria': i['InstanceMatchCriteria'],
                        'InstancePlatform': i['InstancePlatform'],
                        'InstanceType': i['InstanceType'],
                        'State': i['State'],
                        'Tags': i['Tags'],
                        'Tenancy': i['Tenancy'],
                        'TotalInstanceCount': i['TotalInstanceCount']
                    })

    return var_list


# Get Lightsail Instances Function
def get_all_lightsail(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_lightsail = create_boto_client(
        account_number, region, 'lightsail', cross_account_role)

    # Page all reservations
    paginator = client_lightsail.get_paginator('get_instances')

    for page in paginator.paginate():
        for i in page['instances']:
            var_list.append(
                {
                    'EntryType': 'lightsail',
                    'AccountNumber': str(account_number),
                    'Region': str(region),
                    'AvailabilityZone': str(i['location']['availabilityZone']),
                    'Name': str(i['name']),
                    'CreateDate': str(i['createdAt']),
                    'Blueprint': str(i['blueprintName']),
                    'RAM in GB': str(i['hardware']['ramSizeInGb']),
                    'vCPU': str(i['hardware']['cpuCount']),
                    'SSD in GB': str(i['hardware']['disks'][0]['sizeInGb']),
                    'Public IP': str(i['publicIpAddress']),
                })

    return var_list


# Get Organizations Function
def get_organizations(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_org = create_boto_client(
        account_number, region, 'organizations', cross_account_role)

    # Page all org
    paginator = client_org.get_paginator('list_accounts')

    for page in paginator.paginate():
        for i in page['Accounts']:
            if i['Status'] == 'ACTIVE':
                var_list.append(
                    {
                        'AccountNumber': str(i['Id']),
                        'Arn': str(i['Arn']),
                        'Region': 'us-east-1',
                        'EntryType': 'org',
                        'Name': str(i['Name']),
                        'Email': str(i['Email']),
                        'Status': i['Status']
                    })

    return var_list


# Get VPC Function
def get_all_vpc(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_ec2 = create_boto_client(
        account_number, region, 'ec2', cross_account_role)

    # Page all vpc's
    paginator = client_ec2.get_paginator('describe_vpcs')

    for page in paginator.paginate():
        for i in page['Vpcs']:
            var_list.append(
                {
                    'EntryType': 'vpc',
                    'AccountNumber': str(account_number),
                    'Region': str(region),
                    'CidrBlock': str(i['CidrBlock']),
                    'VpcId': str(i['VpcId']),
                    'DhcpOptionsId': i['DhcpOptionsId'],
                    'InstanceTenancy': i['InstanceTenancy']
                })

    return var_list


# Get All Network Interfaces Function
def get_all_network_interfaces(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_ec2 = create_boto_client(
        account_number, region, 'ec2', cross_account_role)

    # Page all vpc's
    paginator = client_ec2.get_paginator('describe_network_interfaces')

    for page in paginator.paginate():
        for i in page['NetworkInterfaces']:
            var_list.append(
                {
                    'EntryType': 'network-interfaces',
                    'PrivateIpAddress': str(i.get('PrivateIpAddress', ' ')),
                    'PublicIp': str(i.get('Association', {}).get('PublicIp', ' ')),
                    'AccountNumber': str(account_number),
                    'Region': str(region),
                    'Status': str(i.get('Status', ' ')),
                    'AttStatus': str(i.get('Attachment', {}).get('Status', ' ')),
                    'InterfaceType': str(i.get('InterfaceType', ' ')),
                    'NetworkInterfaceId': str(i.get('NetworkInterfaceId', ' ')),
                    'Description': str(i.get('Description', ' '))
                })

    return var_list


# Get Subnet Function
def get_all_subnets(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_ec2 = create_boto_client(
        account_number, region, 'ec2', cross_account_role)

    # No paginator for subnets
    # paginator = client_ec2.get_paginator('describe_subnets')
    result = client_ec2.describe_subnets()

    for i in result['Subnets']:
        var_list.append(
            {
                'EntryType': 'subnet',
                'AccountNumber': str(account_number),
                'Region': region,
                'CidrBlock': str(i['CidrBlock']),
                'AvailabilityZone': i['AvailabilityZone'],
                'AvailabilityZoneId': i['AvailabilityZoneId'],
                'SubnetId': str(i['SubnetId']),
                'VpcId': str(i['VpcId']),
                'SubnetArn': str(i['SubnetArn']),
                'AvailableIpAddressCount': i['AvailableIpAddressCount']
            })

    return var_list


# Get Reserved Instances
def get_all_ris(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_ec2 = create_boto_client(
        account_number, region, 'ec2', cross_account_role)

    # No paginator for reservations
    # paginator = client_ec2.get_paginator('')
    result = client_ec2.describe_reserved_instances()

    for i in result['ReservedInstances']:
        # only get active ones
        if i['State'] == 'active':
            var_list.append(
                {
                    'EntryType': 'ri',
                    'AccountNumber': str(account_number),
                    'InstanceCount': str(i['InstanceCount']),
                    'InstanceType': i['InstanceType'],
                    'Scope': i['Scope'],
                    'ProductDescription': str(i['ProductDescription']),
                    'ReservedInstancesId': str(i['ReservedInstancesId']),
                    'Start': str(i['Start']),
                    'End': str(i['End']),
                    'InstanceTenancy': i['InstanceTenancy'],
                    'OfferingClass': i['OfferingClass']
                })

    return var_list


# Get S3 Buckets
def get_all_s3_buckets(account_number, region, cross_account_role):

    # Init
    var_list = []

    # Use boto3 on source account
    client_s3 = create_boto_client(
        account_number, region, 's3', cross_account_role)

    # No paginator for listing buckets
    # paginator = client_ec2.get_paginator('')
    result = client_s3.list_buckets()

    for i in result['Buckets']:
        var_list.append(
            {
                'Name': str(i['Name']),
                'EntryType': 's3-buckets',
                'AccountNumber': str(account_number),
                'Region': 'us-east-1',
                'CreationDate': str(i['CreationDate'])
            })

    return var_list


# Get data sitting in DynamoDB for each account
def get_current_table(account_number, entry_type, region):

    try:
        # Scan dynamo for all data
        response = table.query(
            IndexName='EntryType-index',
            KeyConditionExpression=Key('EntryType').eq(entry_type),
            FilterExpression=Attr('AccountNumber').eq(account_number) &
            Attr('Region').eq(region)
        )

        print(f"items from db query: {response['Items']}")
        return response['Items']

    except ClientError as e:
        print(f'Error: failed to query dynamodb table... {e}')
    except Exception as e:
        print(f'Error: failed to query dynamodb table...{e}')


# Get data sitting in DynamoDB without account look up
def get_current_table_without_account(entry_type, region):

    try:
        # Scan dynamo for all data
        response = table.query(
            IndexName='EntryType-index',
            KeyConditionExpression=Key('EntryType').eq(entry_type),
            FilterExpression=Attr('Region').eq(region)
        )

        print(f"items from db query: {response['Items']}")
        return response['Items']

    except ClientError as e:
        print(f'Error: failed to query dynamodb table...{e}')
    except Exception as e:
        print(f'Error: failed to query dynamodb table...{e}')


# DynamoDB Create Item
def dynamo_create_item(dynamodb_item):

    try:

        # Put item
        response = table.put_item(Item=dynamodb_item)

        print(f'Sucessfully added {dynamodb_item}')
        return response

    except ClientError as e:
        print(f'Error: failed to add {dynamodb_item} - {e}')
    except Exception as e:
        print(f'Error: creating item {dynamodb_item} - {e}')


# DynamoDB Delete Item
def dynamo_delete_item(dynamodb_item):

    try:

        response = table.delete_item(
            Key={
                'Id': dynamodb_item
            })

        print(f'Sucessfully deleted {dynamodb_item}')
        return response

    except ClientError as e:
        print(f'Error: Failed ON ID: {dynamodb_item} - {e}')


# delete all items in table, function not used but good for testing
def dynamo_delete_all_items():
    scan = table.scan(
        ProjectionExpression='#k',
        ExpressionAttributeNames={
            '#k': 'Id'
        }
    )

    with table.batch_writer() as batch:
        for each in scan['Items']:
            batch.delete_item(Key=each)


# compare lists in dynamodb and boto3 calls
def compare_lists_and_update(boto_list, dynamo_list, pop_list):

    # remove Id key to compare current boto calls
    for i in pop_list:
        i.pop('Id')

    if len(boto_list) >= 1:
        for r in boto_list:
            if r not in pop_list:
                print('new item, updating entries now...')
                r.update({'Id': str(uuid.uuid4())})
                # Strip empty values
                strip_empty_values = {k: v for k, v in r.items() if v}
                dynamo_create_item(strip_empty_values)
            else:
                print('no update needed...')
    else:
        # Boto list has no values
        print('list empty, skipping')

    if len(dynamo_list) >= 1:
        for i in dynamo_list:
            old_id = i['Id']
            i.pop('Id')
            if i not in boto_list:
                print('deleting entry as not current or present in boto call')
                i.update({'Id': old_id})
                dynamo_delete_item(i['Id'])
            else:
                print('item is in boto list, skipping')
    else:
        # Boto list has no values
        print('list empty, skipping')


# Reply message
def reply(message, status_code):

    return {
        'statusCode': str(status_code),
        'body': json.dumps(message, cls=DecimalEncoder),
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Credentials': 'true',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Origin, X-Requested-With, Content-Type, Accept'
        },
    }


# Logic to compare what current boto see's vs whats in dynamodb
def compare_and_update_function(account_number, region, sqs_fun, cross_account_role):
    print('printing event....')

    # init
    current_boto_list = []
    dynamo_list = []
    pop_dynamo = []

    # Get Current Boto Data
    if sqs_fun == 'lambda':
        current_boto_list = get_all_lambda(
            account_number, region, cross_account_role)
    if sqs_fun == 'ec2':
        current_boto_list = get_all_ec2(
            account_number, region, cross_account_role)
    if sqs_fun == 'rds':
        current_boto_list = get_all_rds(
            account_number, region, cross_account_role)
    if sqs_fun == 'iam-roles':
        current_boto_list = get_all_iam_roles(
            account_number, 'us-east-1', cross_account_role)
    if sqs_fun == 'iam-users':
        current_boto_list = get_all_iam_users(
            account_number, 'us-east-1', cross_account_role)
    if sqs_fun == 'iam-attached-policys':
        current_boto_list = get_all_iam_attached_policys(
            account_number, 'us-east-1', cross_account_role)
    if sqs_fun == 'odcr':
        current_boto_list = get_all_odcr(
            account_number, region, cross_account_role)
    if sqs_fun == 'lightsail':
        current_boto_list = get_all_lightsail(
            account_number, region, cross_account_role)
    if sqs_fun == 'org':
        current_boto_list = get_organizations(
            account_number, region, cross_account_role)
    if sqs_fun == 'vpc':
        current_boto_list = get_all_vpc(
            account_number, region, cross_account_role)
    if sqs_fun == 'network-interfaces':
        current_boto_list = get_all_network_interfaces(
            account_number, region, cross_account_role)
    if sqs_fun == 'subnet':
        current_boto_list = get_all_subnets(
            account_number, region, cross_account_role)
    if sqs_fun == 'ri':
        current_boto_list = get_all_ris(
            account_number, region, cross_account_role)
    if sqs_fun == 's3-buckets':
        current_boto_list = get_all_s3_buckets(
            account_number, 'us-east-1', cross_account_role)

    # Get current data sitting in Dynamo and remove inactive entries
    if sqs_fun == 'org':
        dynamo_list = get_current_table_without_account(
            entry_type=sqs_fun, region='us-east-1')
    else:
        dynamo_list = get_current_table(
            account_number=account_number, entry_type=sqs_fun, region=region)

    # Deep copy instead of double dynamo read
    pop_dynamo = copy.deepcopy(dynamo_list)

    # remove Id key from dynamodb item and check if value has changed.
    compare_lists_and_update(
        boto_list=current_boto_list, dynamo_list=dynamo_list, pop_list=pop_dynamo)


# Default Lambda
def lambda_handler(event, context):

    print(json.dumps(event))

    # message hasn't failed yet
    failed_message = False

    try:
        message = event['Records'][0]
    except KeyError:
        print('No messages on the queue!')

    try:
        message = event['Records'][0]
        print(json.dumps(message))
        function = message['messageAttributes']['Function']['stringValue']
        account_number = message['messageAttributes']['AccountNumber']['stringValue']
        region = message['messageAttributes']['Region']['stringValue']
        receipt_handle = event['Records'][0]['receiptHandle']

        print(f'function passed is: {function}')

        # Try run each function
        try:

            # Lambda logic
            compare_and_update_function(
                account_number, region, function, cross_account_role)


        except ClientError as e:
            print(
                f'Error: with {function}, in account {account_number}, in region {region} - {e}')
            failed_message = True
            raise e
        except Exception as e:
            print(
                f'Error: with {function}, in account {account_number}, in region {region} - {e}')
            failed_message = True
            raise e


    except ClientError as e:
        print(f'Error: on processing message, {e}')
        failed_message = True
        raise e
    except Exception as e:
        print(f'Error: on processing message, {e}')
        failed_message = True
        raise e


    # message must have passed, deleting
    if failed_message is False:
        client_sqs.delete_message(
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
        )
