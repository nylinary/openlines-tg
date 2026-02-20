from b24pysdk import BitrixTokenLocal, BitrixAppLocal
from b24pysdk import Client

bitrix_app = BitrixAppLocal(
  domain="b24-gko4ik.bitrix24.ru", 
  client_id="local.6992ed7cce7125.09474713", 
  client_secret="NJDySrCkZBwCb5S6Ss6RiMM47sN6qNuFxbfvrmoFs6iVMhy5lr",
)

bitrix_token = BitrixTokenLocal(
    auth_token="79f396690080f35e008099000000001b00000782ae0c1c5f03bcf2ac4f14f8cd28e57d",
    refresh_token="6972be690080f35e008099000000001b000007349bf6ae5fca65651b93f24201127347",  # optional parameter
    bitrix_app=bitrix_app,
)

client = Client(bitrix_token)


request = client.crm.lead.update