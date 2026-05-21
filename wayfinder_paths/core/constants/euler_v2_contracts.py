from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

# Euler Vault Kit (EVK / eVault), EulerEarn, and EulerSwap deployments.
#
# Source of truth:
# https://raw.githubusercontent.com/euler-xyz/euler-interfaces/master/EulerChains.json
#
# Notes:
# - The "vault" address is the market itself (ERC-4626 share token).
# - EVC (Ethereum Vault Connector) is the recommended entrypoint for
#   advanced state-changing EVK operations.
# - Perspective-based verified vault discovery is deprecated in Euler docs;
#   retain addresses for on-chain compatibility and use euler-labels / V3 API
#   for current off-chain discovery where possible.
EULER_V2_REGISTRY_SOURCE_URL = "https://raw.githubusercontent.com/euler-xyz/euler-interfaces/master/EulerChains.json"


def _cs(address: str) -> str:
    return to_checksum_address(address)


EULER_V2_BY_CHAIN: dict[int, dict[str, Any]] = {
    1: {
        "network": "ethereum",
        "status": "production",
        "evc": _cs("0x0C9a3dd6b8F28529d72d7f9cE918D493519EE383"),
        "evault_factory": _cs("0x29a56a1b8214D9Cf7c5561811750D5cBDb45CC8e"),
        "evault_implementation": _cs("0x8Ff1C814719096b61aBf00Bb46EAd0c9A529Dd7D"),
        "euler_earn_factory": _cs("0x59709B029B140C853FE28d277f83C3a65e308aF4"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x4cD6BF1D183264c02Be7748Cb5cd3A47d013351b"),
        "balance_tracker": _cs("0x0D52d06ceB8Dcdeeb40Cfd9f17489B350dD7F8a3"),
        "sequence_registry": _cs("0xEADDD21618ad5Deb412D3fD23580FD461c106B54"),
        "lenses": {
            "vault_lens": _cs("0x7427E9Ef64BBe73D40BBcF455D50d215E50f3177"),
            "account_lens": _cs("0xA60c4257c809353039A71527dfe701B577e34bc7"),
            "utils_lens": _cs("0xbeF9B644B15bA33bc21324365F148b13aBfcC071"),
            "oracle_lens": _cs("0x30E6dFB84782A31d561536f64F47231451F7b48A"),
            "irm_lens": _cs("0x061b6b0bA1B552006556C278FC8798D1e20F807a"),
            "euler_earn_vault_lens": _cs("0x20954C32Bc063a125036b2563ca74fa98b5013D9"),
        },
        "perspectives": {
            "governed": _cs("0xC0121817FF224a018840e4D15a864747d36e6Eb2"),
            "evk_factory": _cs("0xB30f23bc5F93F097B3A699f71B0b1718Fc82e182"),
            "ungoverned_0x": _cs("0xb50a07C2B0F128Faa065bD18Ea2091F5da5e7FbF"),
            "ungoverned_nzx": _cs("0x600bBe1D0759F380Fea72B2e9B2B6DCb4A21B507"),
            "euler_earn_factory": _cs("0xA45895144F2b6E7E6D2fCAFfe6eA19E86aa1667E"),
            "euler_earn_governed": _cs("0x492e9FE1289d43F8bB6275237BF16c9248C74D44"),
            "edge_factory": _cs("0x8c7543f83D3d295F68447792581F73d7d5D4d788"),
            "escrowed_collateral": _cs("0x4e58BBEa423c4B9A2Fc7b8E58F5499f9927fADdE"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0xb013be1D0D380C13B58e889f412895970A2Cf228"),
            "euler_swap_v1_implementation": _cs(
                "0xc35a0FDA69e9D71e68C0d9CBb541Adfd21D6B117"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x208fF5Eb543814789321DaA1B5Eb551881D16b06"
            ),
            "euler_swap_v2_factory": _cs("0xD05213331221fAB8a3C387F2affBb605Bb04DF5F"),
            "euler_swap_v2_implementation": _cs(
                "0x8B0E044E364F2cE913799d53b300e15A6974DC97"
            ),
            "euler_swap_v2_periphery": _cs(
                "0xD3a349EE0A21eA0A7E9513ac236ae614b5FD513E"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x5171Aed04Fa9551DB484F07c853F252Bc6F53b63"
            ),
            "euler_swap_v2_registry": _cs("0x5FcCB84363F020c0cADE052C9c654aABF932814A"),
        },
        "periphery": {
            "swapper": _cs("0x719F8b330CcA71cb6195D032A43194C7D3F9Fb45"),
            "swap_verifier": _cs("0x786c900d7D348662703C38B46f24c1cda2C582AB"),
            "euler_earn_public_allocator": _cs(
                "0x8fdCb80a2894F0dC052c8d52D22544DC90274800"
            ),
            "external_vault_registry": _cs(
                "0xB3b30ffb54082CB861B17DfBE459370d1Cc219AC"
            ),
            "fee_flow_controller": _cs("0xFcd3Db06EA814eB21C84304fC7F90798C00D1e32"),
            "fee_flow_controller_util": _cs(
                "0x653eD9b915c7e7C413e7b2a2B6d83dFe02dd36DF"
            ),
            "irm_registry": _cs("0x0a64670763777E59898AE28d6ACb7f2062BF459C"),
            "oracle_adapter_registry": _cs(
                "0xA084A7F49723E3cc5722E052CF7fce910E7C5Fe6"
            ),
            "oracle_router_factory": _cs("0x70B3f6F61b7Bf237DF04589DdAA842121072326A"),
        },
        "external_vault_registry": _cs("0xB3b30ffb54082CB861B17DfBE459370d1Cc219AC"),
    },
    8453: {
        "network": "base",
        "status": "production",
        "evc": _cs("0x5301c7dD20bD945D2013b48ed0DEE3A284ca8989"),
        "evault_factory": _cs("0x7F321498A801A191a93C840750ed637149dDf8D0"),
        "evault_implementation": _cs("0x30a9A9654804F1e5b3291a86E83EdeD7cF281618"),
        "euler_earn_factory": _cs("0x75F49a2621b6DeC6a5baB22ce961bF3e676EFAE6"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x1D4b9e6ACACdc82Dd9E903C3F4431558Af32C4A9"),
        "balance_tracker": _cs("0x029fDEe85BEdB0553D6fdc538546586641DD7438"),
        "sequence_registry": _cs("0xfE9011FD097cd35866b9e4740BBC88B4ef26E3ba"),
        "lenses": {
            "vault_lens": _cs("0x601F023CD063324DdbCADa69460e969fb97e98b9"),
            "account_lens": _cs("0xe6b05A38D6a29D2C8277fA1A8BA069F1693b780C"),
            "utils_lens": _cs("0x506F74991664dA79b4f407e80adF118c76307c8E"),
            "oracle_lens": _cs("0xCE85cC424d12B8074bacB81c84dA7C6DA317c4D3"),
            "irm_lens": _cs("0xc159d463E7Cdb2C4bA8D4C0C877127A1fCdf33dC"),
            "euler_earn_vault_lens": _cs("0x0BBf9eE761bFF1c4d64dB608781D5e3beFeed875"),
        },
        "perspectives": {
            "governed": _cs("0xafC8545c49DF2c8216305922D9753Bf60bf8c14A"),
            "evk_factory": _cs("0xFEA8e8a4d7ab8C517c3790E49E92ED7E1166F651"),
            "ungoverned_0x": _cs("0x24F2b095df7c76266fd037b847360f69eD591549"),
            "ungoverned_nzx": _cs("0xFff2dA17172588629Adf5BDEF275d9AbEBbA39Bd"),
            "euler_earn_factory": _cs("0x6BE13d0bb485289D39536458afEfbB32312a5179"),
            "euler_earn_governed": _cs("0x08B817C17d84DF89AA371084D910081a5Cc04724"),
            "edge_factory": _cs("0x6ECaCa741ec5013fc709292443b5F43A93a1deCe"),
            "escrowed_collateral": _cs("0x977590fA311755DA2fa1421c1A944520b684f90F"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0xf0CFe22d23699ff1B2CFe6B8f706A6DB63911262"),
            "euler_swap_v1_implementation": _cs(
                "0x3Ce63C16CB719a0c755DA25cd5dD35170A00424f"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x18e5F5C1ff5e905b32CE860576031AE90E1d1336"
            ),
            "euler_swap_v2_factory": _cs("0x6C5f4c239ceD289447737EAB8eEA64523bd9c05E"),
            "euler_swap_v2_implementation": _cs(
                "0x6F1C1a3eAFbB1b345AD5662b0374a9Bf0E4785af"
            ),
            "euler_swap_v2_periphery": _cs(
                "0xA564dAe65eA7B1ce049AbACFC4Cb1A32C93e127c"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x78bCCFA312432cE84d0B818796eE33f9192A284d"
            ),
            "euler_swap_v2_registry": _cs("0x35D410A5052c7362eCdD72cFb65651A71adFaf61"),
        },
        "periphery": {
            "swapper": _cs("0xd54d9Fc684169287f34DA6d57Aa002B424eEbC05"),
            "swap_verifier": _cs("0xF8B2d2BA412E24235eAaDa8d3050202898455455"),
            "euler_earn_public_allocator": _cs(
                "0x0dFFc3A53693bCd8e42FAd9be94fB8f1Fb64A8EE"
            ),
            "external_vault_registry": _cs(
                "0x6A60B3E561F0a7d9587F3210426FeC882224dF2d"
            ),
            "fee_flow_controller": _cs("0xbF4906E2F20362c3d746F7eFfF54abB8282902ed"),
            "fee_flow_controller_util": _cs(
                "0xBb218B381dd403B4F5ED6D60BE0cf4134c35a29f"
            ),
            "irm_registry": _cs("0x384926132a5E516Bc8f79e6d7cd4301B6a887DfC"),
            "oracle_adapter_registry": _cs(
                "0x3cD76476bB7933A99Fa5bAa05446e71e07CDe0ca"
            ),
            "oracle_router_factory": _cs("0xA9287853987B107969f181Cce5e25e0D09c1c116"),
        },
        "external_vault_registry": _cs("0x6A60B3E561F0a7d9587F3210426FeC882224dF2d"),
    },
    1923: {
        "network": "swell",
        "status": "production",
        "evc": _cs("0x08739CBede6E28E387685ba20e6409bD16969Cde"),
        "evault_factory": _cs("0x238bF86bb451ec3CA69BB855f91BDA001aB118b9"),
        "evault_implementation": _cs("0x70f1286239228B28A047c727C2df390045299486"),
        "euler_earn_factory": _cs("0x3073e1B42f8Cc933f2d678DdA10acDE51F4E49a3"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x6682Af2820633067A1de5bE99b2DCb2d38F1e241"),
        "balance_tracker": _cs("0x9fb7215ef6297498D6807caf9f3aC8BA3154db29"),
        "sequence_registry": _cs("0xC4589C6199516F96B422D91020563Fe65b28918e"),
        "lenses": {
            "vault_lens": _cs("0x8A4925517420b906b1703ba5bc230CCF6468B24e"),
            "account_lens": _cs("0x8fE9A01F035B2C6891fD4F70f489A96dc746a08C"),
            "utils_lens": _cs("0x219d787A10a7C13e2052B0D8070851d8f8E3693e"),
            "oracle_lens": _cs("0x2A53414cb2F9A577a698c38D81d2b9546F25D3a8"),
            "irm_lens": _cs("0x1f2FE3Cda7D9e66b835dcE7787534E9C9035057f"),
            "euler_earn_vault_lens": _cs("0x45eAFe502d50f4575C8eAeFB13891C7D200e05c7"),
        },
        "perspectives": {
            "governed": _cs("0xda258aB9569d0156B99943aDC4083E542F70a6f1"),
            "evk_factory": _cs("0x96070bE9d3dFb045c6C96D35CeCc70Aa2940c756"),
            "ungoverned_0x": _cs("0x96367505890EF888c0C92E19a9814fa27B461549"),
            "ungoverned_nzx": _cs("0x2671dA0a4539886cDd4E40096fF1A70b45fc7289"),
            "euler_earn_factory": _cs("0xC7ce91e3fd0cfc81CB0A85c63e590f8265223531"),
            "euler_earn_governed": _cs("0xD5491C7Da8274016244aF8a0bD3998BCc43e4b24"),
            "edge_factory": _cs("0x05cf50742e4457cD7CB2f191c03a61ae95CF3C30"),
            "escrowed_collateral": _cs("0x5757C89D418e35EF1a0CE3A06258A1Fc8D3d31b2"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x976dd85654B3b2f9fb66280ACE30Cab7C81a2130"),
            "euler_swap_v1_implementation": _cs(
                "0x3620dAb0DB5595479a4D5408595D48FbE48CeA2A"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x34932C04c3d27c2BD7aCd0B5d203bfd65a17f481"
            ),
            "euler_swap_v2_factory": _cs("0x980cd01dB708C2B008cF8076aa856fcAeF60698c"),
            "euler_swap_v2_implementation": _cs(
                "0xCB72817170a9d0f136fcB764f55BEEC638C25acb"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x74F7726eF6D8107403a6c581B985D27ED0561f9D"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x6FB6C91e369fD151eB343de3BE76F3617FbFfDf2"
            ),
            "euler_swap_v2_registry": _cs("0x3327428147E0cE4Cb185A82F756CaE0D429dbd2c"),
        },
        "periphery": {
            "swapper": _cs("0x7212F011bbB3d1a04F20a548b0048cEad4dA9f42"),
            "swap_verifier": _cs("0x605280f2F939255Ab36FaFdBC654dE3cfbD5c616"),
            "euler_earn_public_allocator": _cs(
                "0x0a13C9fc4613Cae202138F4E1C2b9125A20562E8"
            ),
            "external_vault_registry": _cs(
                "0x575fcBb7a9f72F8550E578f8fEed6Ac40e0b3b5C"
            ),
            "fee_flow_controller": _cs("0xA93Ff8C4CC2Ba56Ee182B70bb07F2C75DA249879"),
            "fee_flow_controller_util": _cs(
                "0x974417020092194d5f5119e5879Ef77B8F46B180"
            ),
            "irm_registry": _cs("0x25fDdEae07949101fb4D060b95aB999fcAe547D5"),
            "oracle_adapter_registry": _cs(
                "0x049B9404B9cB8E0A02191e563faEFEAA8D87E619"
            ),
            "oracle_router_factory": _cs("0x0135fC2605ff2C89E550C2d4C7d75068A4782B43"),
        },
        "external_vault_registry": _cs("0x575fcBb7a9f72F8550E578f8fEed6Ac40e0b3b5C"),
    },
    146: {
        "network": "sonic",
        "status": "production",
        "evc": _cs("0x4860C903f6Ad709c3eDA46D3D502943f184D4315"),
        "evault_factory": _cs("0xF075cC8660B51D0b8a4474e3f47eDAC5fA034cFB"),
        "evault_implementation": _cs("0x11f95aaa59F1AD89576c61E3C9Cd24DF1FdCF46f"),
        "euler_earn_factory": _cs("0x3397ec7d28cF645A017869Fe4B41c75f5B0b75a8"),
        "permit2": _cs("0xB952578f3520EE8Ea45b7914994dcf4702cEe578"),
        "protocol_config": _cs("0xc2f9FE90bd17e017898b6EfDaa73c34Fddde299e"),
        "balance_tracker": _cs("0xe6E4687C35429942391AfE42CDdECba857531492"),
        "sequence_registry": _cs("0x6F417AaEc1D41dB692307269acDA019Ce5F10b0e"),
        "lenses": {
            "vault_lens": _cs("0x575e3Aa8243aD6CD7Cea1cD2C36F5E577B1f745c"),
            "account_lens": _cs("0x99Cf844584BBFa12E6b76a9FD3C08C2Dd99F87C4"),
            "utils_lens": _cs("0xA546d41f25349905e8329943d42Ba9F00073Edea"),
            "oracle_lens": _cs("0x9e54bF56F4E5219A7D5A4386255B1943D01b6F36"),
            "irm_lens": _cs("0x811Debb6EEcF205f469c87883Bf4b95b41533961"),
            "euler_earn_vault_lens": _cs("0x0832b2a2060F878D3BF09eB3E600C982DD1e0fbf"),
        },
        "perspectives": {
            "governed": _cs("0x93478469b049e75B8d20b6d2c5A8da84E35f14D0"),
            "evk_factory": _cs("0x69D2403d9a0715CDc89AcB015Ec2AfB200C4f6BD"),
            "ungoverned_0x": _cs("0x770500Ee92d2C395Aa39f2C573A08D78D5FF8090"),
            "ungoverned_nzx": _cs("0x2a75a1D4e4bba15e74693141f8D75f206BFa2967"),
            "euler_earn_factory": _cs("0xeFe3F05a2270B1D0739f3ed517127A3d1a70B97a"),
            "euler_earn_governed": _cs("0x044Dc2d44BC443c00f615Cc453501F881E01E021"),
            "edge_factory": _cs("0xBB3C87743126F6CD025569be8af89Aeb43384ef1"),
            "escrowed_collateral": _cs("0xb70DbFBBaB2B9F01A86B397F0954beA2EAF3Bce5"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x94041db6deC15f79666B07846c13e6F7341b4a80"),
            "euler_swap_v1_implementation": _cs(
                "0x4D57F54582b333E4184A3cF40d1D61FE6D70c35D"
            ),
            "euler_swap_v1_periphery": _cs(
                "0xb2237DC86B184e50Fc2F8b028B2b7AE192ef2566"
            ),
            "euler_swap_v2_factory": _cs("0x46D23b2d948b159859A5BB9C96c3190F4b43Ebb6"),
            "euler_swap_v2_implementation": _cs(
                "0x1C266a986B6AfA7EbA68263c5323a0bF0fe4F2a4"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x4e4e524613d840D2D30B694F363b5b1931e82A75"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x8653d1B50AA6adaaD64Dc140588dBC8c11141581"
            ),
            "euler_swap_v2_registry": _cs("0x0601a38324D3cde22EBD531c799Ad318a6B8CF93"),
        },
        "periphery": {
            "swapper": _cs("0x2cb79cdA6Bb09A901177D5227b4aA1584Dbcfc9B"),
            "swap_verifier": _cs("0x84354221A6C432a9907F4D0777d8e794646206da"),
            "euler_earn_public_allocator": _cs(
                "0x82b27b528DA0516E653e02e5f870853d22Cbc6Df"
            ),
            "external_vault_registry": _cs(
                "0x650737Bf472588A04530494189c3c30eaF5f6C50"
            ),
            "fee_flow_controller": _cs("0xD3Cf3Ec3D7849F2C7Bb9Ff5a8662Ae36a177bEb8"),
            "fee_flow_controller_util": _cs(
                "0xa8bA1b304aDA6A7E4bcc8814071e905712f2E3AC"
            ),
            "irm_registry": _cs("0xeEF91153b27fabF42a2F88e753285D0aFb736d09"),
            "oracle_adapter_registry": _cs(
                "0x93Fd7A2b4E6BEa3c35D06468a7Bd7b0eA202d075"
            ),
            "oracle_router_factory": _cs("0xc5b9B95a769C24c18c344c2659db61a0AdFB736E"),
        },
        "external_vault_registry": _cs("0x650737Bf472588A04530494189c3c30eaF5f6C50"),
    },
    60808: {
        "network": "BOB",
        "status": "production",
        "evc": _cs("0x59f0FeEc4fA474Ad4ffC357cC8d8595B68abE47d"),
        "evault_factory": _cs("0x046a9837A61d6b6263f54F4E27EE072bA4bdC7e4"),
        "evault_implementation": _cs("0x32CFc56917C0025501b34C43f7FE767Ef1EDE3a2"),
        "euler_earn_factory": _cs("0x8F01c6640A1c0a6085C79843F861fF0F89b9fED6"),
        "permit2": _cs("0xCbe9Be2C87b24b063A21369b6AB0Aa9f149c598F"),
        "protocol_config": _cs("0x94047C7daF06a6DE4049365cFa95fb4389a6F9Fe"),
        "balance_tracker": _cs("0x5a3828beA292E5f29725Fa449F9113Cb5E60ADF8"),
        "sequence_registry": _cs("0xf4C097718c64B6B0A75Cd9e0EF348fD6F176bE67"),
        "lenses": {
            "vault_lens": _cs("0x6213F480683e8ABDf8A78c0Fa3190FEf7EF1AaD6"),
            "account_lens": _cs("0x41FE40e10268decF2D25c60aDf60469EE94E8771"),
            "utils_lens": _cs("0xbe662AF201D6B7a4AFA96A8C7c4b4eCB096bc9d3"),
            "oracle_lens": _cs("0xd9697A7Cab800cAaE7F1E9B4db64385D971a5F13"),
            "irm_lens": _cs("0xf2653b811FdAD3A704BFB17b5ef17Ab7a52877EE"),
            "euler_earn_vault_lens": _cs("0xf62E75A45B1D099CaF4B04C4DA468385A2032e55"),
        },
        "perspectives": {
            "governed": _cs("0xed62ebA9552dF86b5F7d995eD00C06494bBbB638"),
            "evk_factory": _cs("0x05B98f64A31A33666cC9D2B32046a6Ca42699823"),
            "ungoverned_0x": _cs("0x878343fc7AA3F3eC841D6C6A0e942B7209EF0D30"),
            "ungoverned_nzx": _cs("0x6853213a8c0b66b7148B87E8D5cCfc580F60c077"),
            "euler_earn_factory": _cs("0x1224ae61765090784731c8b36CEB2f7946eBE308"),
            "euler_earn_governed": _cs("0x7b4065698dF1dd0DF7ad4876A5050657295f836a"),
            "edge_factory": _cs("0xb24B26E3091249b6d3FCed5AA30213e694f72474"),
            "escrowed_collateral": _cs("0x44dcB151f7b091eA6C9090Ba7ad94C3e479bFE63"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0xE25B3cdA6fccAcbD794aEA64eE1B496d7b441644"),
            "euler_swap_v1_implementation": _cs(
                "0x334eac29ffAc27E6BC3484A738DAf520359698F0"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x199cC7C8606088bc22D82CDae2D7EE7F5F99ec9F"
            ),
            "euler_swap_v2_factory": _cs("0xa077991e2929d97f29fE39372E736FC118a4FAd3"),
            "euler_swap_v2_implementation": _cs(
                "0x90bd38E89726BdCf42E07D88B23c2A493cb3877a"
            ),
            "euler_swap_v2_periphery": _cs(
                "0xaEAab95eE90196E20fD2a5348643cCa0EF2b038e"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x6e5dF960eccD2Bf8818526A88f6E7da99a5379d7"
            ),
            "euler_swap_v2_registry": _cs("0xf33F4e20905801D55531b38749727954D0152d3D"),
        },
        "periphery": {
            "swapper": _cs("0xB5949BcaF4BC1bC0ef2D132A4A2Ec5cf4D5934CD"),
            "swap_verifier": _cs("0x5cb5C6F2c0147a337d476A71c2d2897f2B3A8f80"),
            "euler_earn_public_allocator": _cs(
                "0xB5Daee4a8AD1388B3D72C1367b8BA63DfB4AAbf5"
            ),
            "external_vault_registry": _cs(
                "0x28029B4De813866A4F7F03AeE4445732F02B3B09"
            ),
            "fee_flow_controller": _cs("0xcb3c0D131C64265099868F847face425499785A8"),
            "fee_flow_controller_util": _cs(
                "0x4DB5a802E5dc5BA51162393Ebb79CD814F48B7aD"
            ),
            "irm_registry": _cs("0x709B4e5B081dD64101ebf6Fd111b2d5B671d8c88"),
            "oracle_adapter_registry": _cs(
                "0xc57d019a9B57FbC9EE81019ef064960B0Dd6C741"
            ),
            "oracle_router_factory": _cs("0xEFCF1F2f09163e3813f5C16346A9F2Aa21ABA74d"),
        },
        "external_vault_registry": _cs("0x28029B4De813866A4F7F03AeE4445732F02B3B09"),
    },
    80094: {
        "network": "berachain",
        "status": "production",
        "evc": _cs("0x45334608ECE7B2775136bC847EB92B5D332806A9"),
        "evault_factory": _cs("0x5C13fb43ae9BAe8470f646ea647784534E9543AF"),
        "evault_implementation": _cs("0x402598Ac4034D24f2cB37BDb0721A67365aD19BD"),
        "euler_earn_factory": _cs("0x9cbc3030e6d133D1AAa148D598FD82D70263495c"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x51432af61A715DB3D0f20A3691C1E25F9A2c6B05"),
        "balance_tracker": _cs("0x70Fb24bDa46E7cFD447C64bB32180Bc746ba3A71"),
        "sequence_registry": _cs("0x0c9a75E05764775A0cF52bC6cbfE6Cb229bb3901"),
        "lenses": {
            "vault_lens": _cs("0x9e43CC80664Cc5Af5D0a37d821305377ae6911Bb"),
            "account_lens": _cs("0xfC09040C5E26aec5E55a93F6856159A0C28ffDB9"),
            "utils_lens": _cs("0x00b9f08C33cB1C2784515A6b513EE2229f280E46"),
            "oracle_lens": _cs("0x8555B31Ce5ebCD6F8a031ff599728eeb276634d3"),
            "irm_lens": _cs("0x79efC98d5BC75688858787a3613EEE9B1bC875d6"),
            "euler_earn_vault_lens": _cs("0x07dB55756ED3A08e3Ab0e0B66CE42Ac304bd052B"),
        },
        "perspectives": {
            "governed": _cs("0xAE06ad3a165acA82AC4eFEcdE2D3875414C419b2"),
            "evk_factory": _cs("0xEE0CA74F3c60B7e1366e6d64AE2426E5177145cf"),
            "ungoverned_0x": _cs("0x853ea3e0942e74B65D65275b2A2F3237B83A58d8"),
            "ungoverned_nzx": _cs("0xE86e9B82788C1438b95346E7BF180AAf91AFC4bb"),
            "euler_earn_factory": _cs("0xa78AFa313B6897B726f94417FF199210da378585"),
            "euler_earn_governed": _cs("0xff216ceb263b3be308De306dbC67E2D82Ed70ea9"),
            "edge_factory": _cs("0x749943E5Cf06c5Ff64EC475f2f220285520Ab389"),
            "escrowed_collateral": _cs("0x766BF8aa4c90eF8df29aEf5D70C7aba1BC40Ee05"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0xD14c95dc228E8851F63d9b83A0001F4D021B5DFf"),
            "euler_swap_v1_implementation": _cs(
                "0x0e05d236cb6c350935751A73e834A13111998e3c"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x46F951278f52f4798542C51BfB8Df1c165199150"
            ),
            "euler_swap_v2_factory": _cs("0x1A4546b988Ee133F72b7E27a4890355b0a341554"),
            "euler_swap_v2_implementation": _cs(
                "0x9253a3EF2cE8875b7D15Bd2bcd3a405b62a7b0E7"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x5e044DB2Fd14fbB48334b239CfD8530C9b03150B"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0xAe26ca82da91a1157E3cC0B36a9A06f539f4DF24"
            ),
            "euler_swap_v2_registry": _cs("0x8D8B81F0c1be01fa3636d2cD6DeF07474d75e1e9"),
        },
        "periphery": {
            "swapper": _cs("0x83Ee58fE951bb0133F4E30D61863988378CD665E"),
            "swap_verifier": _cs("0xE5cca51c93BF775cc176A45e28487026da777800"),
            "euler_earn_public_allocator": _cs(
                "0x4E7C059099496D56e8662570426991EA63C63C85"
            ),
            "external_vault_registry": _cs(
                "0x73313Bc5aF05187466f42c53eaF4851816bd76CD"
            ),
            "fee_flow_controller": _cs("0x5EAe58dc72E4E374F32eCA2751cC38b573dd82c9"),
            "fee_flow_controller_util": _cs(
                "0x3DB06879907a3311c4b95A19fDb80ead78Bd940d"
            ),
            "irm_registry": _cs("0x5650d5384e86C512c5Ea730D19451D3991086c7D"),
            "oracle_adapter_registry": _cs(
                "0x50742B56E4c563d1cC3956AcBacd975d3f5309d2"
            ),
            "oracle_router_factory": _cs("0x809aB347e6ECb46714917A7796E542c86f75FbF1"),
        },
        "external_vault_registry": _cs("0x73313Bc5aF05187466f42c53eaF4851816bd76CD"),
    },
    43114: {
        "network": "avalanche",
        "status": "production",
        "evc": _cs("0xddcbe30A761Edd2e19bba930A977475265F36Fa1"),
        "evault_factory": _cs("0xaf4B4c18B17F6a2B32F6c398a3910bdCD7f26181"),
        "evault_implementation": _cs("0x29E9b639e165d919FEcf02521F8A9dA0492D4f21"),
        "euler_earn_factory": _cs("0x574B00f5a0C56D370F19fa887a5545d74F52fAC2"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x8564160f30926eA1229DCcf24118c6De155D2e30"),
        "balance_tracker": _cs("0xAf5659428FEF1F6a701FaB46d8f3aF8371A9913D"),
        "sequence_registry": _cs("0x9C38f923baC407C818312EADEf69AdC116fd16FD"),
        "lenses": {
            "vault_lens": _cs("0x7a2A57a0ed6807c7dbF846cc74aa04eE9DFa7F57"),
            "account_lens": _cs("0x08bb803D19e5E2F006C87FEe77c232Dc481cB735"),
            "utils_lens": _cs("0x0004DBd59Af6Ee41fdDa31cbA1F996ea688F9109"),
            "oracle_lens": _cs("0xC5FFCe5f0e6646D93F7E79bD71d268dFC1B7EfD7"),
            "irm_lens": _cs("0x8D990f217879E3C49894024f5D72431DA3Ef656C"),
            "euler_earn_vault_lens": _cs("0xe58989e0E3f20f2e56fD407C6E28fe63675fDdB8"),
        },
        "perspectives": {
            "governed": _cs("0x0d1ABCcBa91F074DeA11AdCc679C61326b6145AC"),
            "evk_factory": _cs("0x4247432b4f9c32e99ecC2Ff7bAdd98783EecFA6F"),
            "ungoverned_0x": _cs("0x299f86BbB552F74Be79A687c565aC52452C0a02d"),
            "ungoverned_nzx": _cs("0xC2675790c775D385425D72652ded5f299Fbb2868"),
            "euler_earn_factory": _cs("0x068f7C1f56B3963Beeb1ef4474bca4dfe83FDc37"),
            "euler_earn_governed": _cs("0x23559eF969252b81d8DA2b86a76D85fb602860Ad"),
            "edge_factory": _cs("0x0C13cf54c341a0F91939685dCB1C9b75c2A6f595"),
            "escrowed_collateral": _cs("0x19747Fb40074F8cC32fd24445fAce1fCe11BD281"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x8A1D3a4850ed7deeC9003680Cf41b8E75D27e440"),
            "euler_swap_v1_implementation": _cs(
                "0x4F4FDeE3568aC31C46634fb2Df3FF44A156Be351"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x31F34124a37f94efd17201A1B88d5008cD444c72"
            ),
            "euler_swap_v2_factory": _cs("0xd80e68B39e4408cb7D6c8E3343Bde46587013F62"),
            "euler_swap_v2_implementation": _cs(
                "0x2836825daeC3D5d8fD3ad71d61f72345bB868110"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x4fef2f7146c0b4e6C0b1433badC6B7a2E1E7ECDb"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x1C0e8b841DA677C685D2a8376773e8A872C1ce5C"
            ),
            "euler_swap_v2_registry": _cs("0xF9f2dF8A5Cc71a0424dfA9EbdfdfF8A082C19184"),
        },
        "periphery": {
            "swapper": _cs("0x065D7B495D25436E492fE57116665894Bfe17157"),
            "swap_verifier": _cs("0x768B74A19115316c1A782fFa335FdfBb66278174"),
            "euler_earn_public_allocator": _cs(
                "0x2524762ddb853AB1e572B81E5E6377a8a1536aA5"
            ),
            "external_vault_registry": _cs(
                "0xe41338Ccac8121fb472817c58c485776E77f3Eea"
            ),
            "fee_flow_controller": _cs("0x95F21cD90057BBdC6fAc3f9b94D06b53C24B278c"),
            "fee_flow_controller_util": _cs(
                "0xBBd929f6c61b59248eD660213C0129a119d54306"
            ),
            "irm_registry": _cs("0x9a05B935c2ABeD87b4d89c3E74dA253ffc49a2c1"),
            "oracle_adapter_registry": _cs(
                "0x0345D8a0Be83834B4611D7D20B661D0Bd2536928"
            ),
            "oracle_router_factory": _cs("0x80528F014E84658e85D3C6D4896A29Fa933Be696"),
        },
        "external_vault_registry": _cs("0xe41338Ccac8121fb472817c58c485776E77f3Eea"),
    },
    56: {
        "network": "BSC",
        "status": "production",
        "evc": _cs("0xb2E5a73CeE08593d1a076a2AE7A6e02925a640ea"),
        "evault_factory": _cs("0x7F53E2755eB3c43824E162F7F6F087832B9C9Df6"),
        "evault_implementation": _cs("0xB236413f1A8Fd4C5D5545ecAaC5e64fF686afe4e"),
        "euler_earn_factory": _cs("0xc456d04E3F43597CC7E5a2AF284fF4C4AdDA0cb1"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0xF524F75ad063919B86d6c5D9242847A44337BFCe"),
        "balance_tracker": _cs("0x2D13C46FE6c8B6c9ad3C5A78eD51b26733caE350"),
        "sequence_registry": _cs("0x7fD287B3AE3Bf2F6C9871a44b6d9de208B0ABBE5"),
        "lenses": {
            "vault_lens": _cs("0x10b088EaE2260e1Ddedc5a3Af95a6B85CfbBd384"),
            "account_lens": _cs("0x9578D17d2e1AA70EA6f9eC8A39967bfD1c6F6217"),
            "utils_lens": _cs("0xfe4e3622c632F98aaF21a08c8b83e02D994c08D4"),
            "oracle_lens": _cs("0x7408034385689733f09072ff4c976C14b0211477"),
            "irm_lens": _cs("0x0c34F8F5CCE64Ae0A437c2112F2940eD48D7923D"),
            "euler_earn_vault_lens": _cs("0x079E485A869d2cEca0dCbB96A8308e6d972aB57f"),
        },
        "perspectives": {
            "governed": _cs("0x775231E5da4F548555eeE633ebf7355a83A0FC03"),
            "evk_factory": _cs("0x9d928D359646dC4249A8d57259d87673F118Ec85"),
            "ungoverned_0x": _cs("0xea19a15182A78e8fFF080F79C769FBB590f4D3E9"),
            "ungoverned_nzx": _cs("0x32581e478819f24434baC9041542770026cE32A7"),
            "euler_earn_factory": _cs("0x4E5e9BCafeA5C68E8D93CAb3cF1833fC0c77b0eF"),
            "euler_earn_governed": _cs("0xEF7599ef1CB0ec48ED6f4174641462D6919A7CE2"),
            "edge_factory": _cs("0x546d1E3C430C712A610Df311727529aa6512c7e5"),
            "escrowed_collateral": _cs("0x65B8Faec13bA76Decc5dc5678bf356954cCd6823"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x3e378e5E339DF5e0Da32964F9EEC2CDb90D28Cc7"),
            "euler_swap_v1_implementation": _cs(
                "0x16BCa43290b77409e6D1c92B929f7A09C0E4EE86"
            ),
            "euler_swap_v1_periphery": _cs(
                "0xa8826Bb29f875Db4c4b482463961776390774525"
            ),
            "euler_swap_v2_factory": _cs("0xA1F83E3d1819C912122A1582B4B6D3d2a1E83bb7"),
            "euler_swap_v2_implementation": _cs(
                "0x90Cb0b67f189a3D914DA00f72070531152DBc85F"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x4258A34923CccFa29948881Cf6Aa8FdAD6338485"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x71dFB7138192B19CDc73487212bf6BB1Ffe3b9A1"
            ),
            "euler_swap_v2_registry": _cs("0xBc0f4dd9B5A10b15e6fA65e939Dbb1f98E7B08B7"),
        },
        "periphery": {
            "swapper": _cs("0x8e39500a6672D701616ED4943a5Cc5C79Ab38643"),
            "swap_verifier": _cs("0xc0126DE6e1615479b357e2Fef6d423FB2FBEe502"),
            "euler_earn_public_allocator": _cs(
                "0xD561479477b03720bF485e91B76574374A646531"
            ),
            "external_vault_registry": _cs(
                "0x74171139d712AE64faA8cEFA524e13fd52826c1b"
            ),
            "fee_flow_controller": _cs("0xE7Ef8C7CcB6aa81e366f0A0ccd89A298d9893E83"),
            "fee_flow_controller_util": _cs(
                "0x20d7B41c7b00DeDBF8Eff88A3C3832B5cF299555"
            ),
            "irm_registry": _cs("0x69e47D24dE839423A94afcD01b88C1683BA4D1D0"),
            "oracle_adapter_registry": _cs(
                "0xd6238D3b8bEEd1C7f424eBF6fc1CcD7fe1b31665"
            ),
            "oracle_router_factory": _cs("0xbe83f65e5e898D482FfAEA251B62647c411576F1"),
        },
        "external_vault_registry": _cs("0x74171139d712AE64faA8cEFA524e13fd52826c1b"),
    },
    130: {
        "network": "unichain",
        "status": "production",
        "evc": _cs("0x2A1176964F5D7caE5406B627Bf6166664FE83c60"),
        "evault_factory": _cs("0xbAd8b5BDFB2bcbcd78Cc9f1573D3Aad6E865e752"),
        "evault_implementation": _cs("0x71d7250732591C41D1BdeB1EA0Ee730E138E0c8b"),
        "euler_earn_factory": _cs("0xD785adD5F081F56616898E45b90dE307e3DC7d3E"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0xdCD02E4eA8cd273498D315AD8c047305f8480656"),
        "balance_tracker": _cs("0xFbD12fbC91311A8f17598b935e35205EAF16Aa75"),
        "sequence_registry": _cs("0x08799a00BC4a74890d65f77828cd2BFbBFcD96dB"),
        "lenses": {
            "vault_lens": _cs("0x441F98bEA2F68ac242A38af062Af7bd5Ad4b61b5"),
            "account_lens": _cs("0xa06b923a85d96c62205fA007435E375e9d0Ce31f"),
            "utils_lens": _cs("0xc7850044db632B35e664f6cF3177Fd7404CA5DbF"),
            "oracle_lens": _cs("0x30100D82EE8Fd7dE7a9762Dce7f08055fdADb9Be"),
            "irm_lens": _cs("0x227cc7C2DA74bE56A24Df0f4cDFFb7F227fc86f8"),
            "euler_earn_vault_lens": _cs("0x3a373AF9759ac6546A6BFa6eAAbb0B8fc1E1d241"),
        },
        "perspectives": {
            "governed": _cs("0x44d781D9f61649fACeeEC919c71C8537531df027"),
            "evk_factory": _cs("0x5A2164C500f4FD26AB037d97A3ed5d0774446c6B"),
            "ungoverned_0x": _cs("0xeEF6CF66abbD88fe97BeE236aac21285158f3a3A"),
            "ungoverned_nzx": _cs("0xcAb8bBe881a13A513770746AF15F7cC884843734"),
            "euler_earn_factory": _cs("0x3E6AEb84434F43C550F72d8F20982fC76a1A4b82"),
            "euler_earn_governed": _cs("0x16F187C4EFCCbbF5B530A9c64447B89c4D73F3F2"),
            "edge_factory": _cs("0x990Bb5D21Cc852687bF95B850a279daF9b6C45D2"),
            "escrowed_collateral": _cs("0x413Cf25A789784e07a428D7fb1e0B43eeF84A4B0"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x45b146BC07c9985589B52df651310e75C6BE066A"),
            "euler_swap_v1_implementation": _cs(
                "0xd91B0bfACA4691E6Aca7E0E83D9B7F8917989a03"
            ),
            "euler_swap_v1_periphery": _cs(
                "0xdAAF468d84DD8945521Ea40297ce6c5EEfc7003a"
            ),
            "euler_swap_v2_factory": _cs("0xf211d70Ed785f0e981E9F3188804Af43734502F1"),
            "euler_swap_v2_implementation": _cs(
                "0x144f1715c673dA83917B09A5B4C23E2d72c8D411"
            ),
            "euler_swap_v2_periphery": _cs(
                "0xAD335516c6E17815d9DD543fBCDFE325F8563E13"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0xeA96Ed6896aB1F00e4Fc28C75D8e6655e56Cef85"
            ),
            "euler_swap_v2_registry": _cs("0x9D9ce1540b986eF77c02F8D40603193852D2E723"),
        },
        "periphery": {
            "swapper": _cs("0xDF3009390D10dC18a8f8B42402F1541c7235DfB4"),
            "swap_verifier": _cs("0xDAd370C74A9Fe7e6bfd55De69Baf81060e51eab4"),
            "euler_earn_public_allocator": _cs(
                "0x68a823a484a9D5A8daBB55c4d4d8006a45E557A9"
            ),
            "external_vault_registry": _cs(
                "0xC0a8dFA92CB9FF9F503803D3bAE2CF19E9c15411"
            ),
            "fee_flow_controller": _cs("0x87BeecC6B609723B2Ef071c20AA756846969240C"),
            "fee_flow_controller_util": _cs(
                "0xfAfc7B37d6389919e5142c8b63203602Cb2a5a92"
            ),
            "irm_registry": _cs("0x01315b1fa7e8A58D641C2c7f538654Fa32E0341f"),
            "oracle_adapter_registry": _cs(
                "0xDc0D3E9119d4ccB7E186E699d1df5cDd7bCa5783"
            ),
            "oracle_router_factory": _cs("0xE551288F0D82C10bBF517DBA66E15C60BF87FE8f"),
        },
        "external_vault_registry": _cs("0xC0a8dFA92CB9FF9F503803D3bAE2CF19E9c15411"),
    },
    42161: {
        "network": "arbitrum",
        "status": "production",
        "evc": _cs("0x6302ef0F34100CDDFb5489fbcB6eE1AA95CD1066"),
        "evault_factory": _cs("0x78Df1CF5bf06a7f27f2ACc580B934238C1b80D50"),
        "evault_implementation": _cs("0x832fF4011A3164ea76ceA06A313EE0B6CD72ba96"),
        "euler_earn_factory": _cs("0xB9B5d62B9fE9E1B505466e75817aB178A1D2ec9d"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x06c1Ab0A1672E8FC7F7D10BD7B869B4116D18a2c"),
        "balance_tracker": _cs("0xbCD29c1B596d9fFAfaa6F90780956b4D3d47832f"),
        "sequence_registry": _cs("0x924C73abAa350800fc22c11ffdFB09641106E3ce"),
        "lenses": {
            "vault_lens": _cs("0x8E0321a0f6d37411136077215ED9A539C1B16258"),
            "account_lens": _cs("0x90a52DDcb232e7bb003DD9258fA1235c553eC956"),
            "utils_lens": _cs("0x2b8011B27013a9BDB00Fc7BD524777c8838e293e"),
            "oracle_lens": _cs("0x3AA8F4B4DB88506DE0E9541f81dFa52178575bDd"),
            "irm_lens": _cs("0x5EB6991404a4Dd8aD2d34f04EF1fcde53C0300aF"),
            "euler_earn_vault_lens": _cs("0x15971F66916d402646ad3DEaE482ccf37b2100ef"),
        },
        "perspectives": {
            "governed": _cs("0xc7693ceEf74Bc7c8Af703c5519F24bB5e6642643"),
            "evk_factory": _cs("0x03a931446F5A7e7ec1D850D8eaF95Ab68Ad9089C"),
            "ungoverned_0x": _cs("0x068789293D461Be145D14BfC0e270941554CAC26"),
            "ungoverned_nzx": _cs("0xfbB90dce4a2aCb5425b96B7886D621DE913c816D"),
            "euler_earn_factory": _cs("0x12241404ea27FA4BF7ECDAD2Cb13A99860d7d4Ac"),
            "euler_earn_governed": _cs("0xeE3de4507cFAc8756634dC5272B4A6BB7f00C49E"),
            "edge_factory": _cs("0x57E2e654d1576a02BeAaf3113C3faA9183c379eD"),
            "escrowed_collateral": _cs("0xD775c4960c3A1B4A6A9962e37ecDe5c6b5fd56Fb"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x7949bE8B154D7B5ce6E75cBfc646AeF3a25970E2"),
            "euler_swap_v1_implementation": _cs(
                "0x04671F895c7d9EAbF33FF1dfF41269E6Fea835D1"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x804485f5B6c293f8d63f697E9662CD4a8765858A"
            ),
            "euler_swap_v2_factory": _cs("0x138AB9B33741B25bb7BcDa466175c8B2E2b96dc4"),
            "euler_swap_v2_implementation": _cs(
                "0xAF6412D58024874b0Ffc4138FfF95fc73b372977"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x223c1a20A6992a0F1E7066eD924619c3156DDA15"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0xA6fCC47f8D930f096F8749C7C7D335871bc71C0D"
            ),
            "euler_swap_v2_registry": _cs("0x99C341F07098ba70aC1130c479103Dc2366dbBD7"),
        },
        "periphery": {
            "swapper": _cs("0x4AaA129FaD81a65Dab41b1fa7e964CBB9B30C848"),
            "swap_verifier": _cs("0xcB4cbC3128b38d6Ca46b7676D2389fAfa6009c1f"),
            "euler_earn_public_allocator": _cs(
                "0x0161FE2CA6ED39b5D0811a94b87AC628677Ae020"
            ),
            "external_vault_registry": _cs(
                "0xFB13aa1d7CFe1C85826f9D5e571589B13b785A6e"
            ),
            "fee_flow_controller": _cs("0xA1585dc7Cd4EF33f7a855fDE39771b37838B0bFE"),
            "fee_flow_controller_util": _cs(
                "0x181042fcaC5926CDC89Cecbcf293Bb3b3ee5eC48"
            ),
            "irm_registry": _cs("0xC66887f15038db0379bb9Feb020Feab0F93D8f0E"),
            "oracle_adapter_registry": _cs(
                "0x3942A72f87Db5Ad9C22d8826FDe15E23b81b1cBf"
            ),
            "oracle_router_factory": _cs("0x22d51Db42A59862D4F8c135C4406AEf9854ABFF3"),
        },
        "external_vault_registry": _cs("0xFB13aa1d7CFe1C85826f9D5e571589B13b785A6e"),
    },
    239: {
        "network": "TAC",
        "status": "production",
        "evc": _cs("0x01F594c66A5561b90Bc782dD0297f294cD668b64"),
        "evault_factory": _cs("0x2b21621b8Ef1406699a99071ce04ec14cCd50677"),
        "evault_implementation": _cs("0x1974899F5d6B5a1f8E63b2e8Ad60e14BAC3E7980"),
        "euler_earn_factory": _cs("0x7670572aa76E6140400A948e7AAFAB0210a86d9f"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x4C3D26D7Eb6D5AA62CFD99624ad4Ff3351E4B129"),
        "balance_tracker": _cs("0x45ff89cD0e976392703048F4A4314A2010ee64b8"),
        "sequence_registry": _cs("0xF7a9F90b5ACb4EE4Cd536940142A04522D28e0Aa"),
        "lenses": {
            "vault_lens": _cs("0x8d7512cb0Bb5AbE888Db345E3C122eDf18D182eF"),
            "account_lens": _cs("0x8A3b3E493733e54977B539A4E475Bf16463ecBD6"),
            "utils_lens": _cs("0xFEA22EFaa9403709Dd28eF36c8C1de1aad6ce137"),
            "oracle_lens": _cs("0xB7b2530A8a545504d35F7502E0bf9Fba59F772D6"),
            "irm_lens": _cs("0x30DD8F6A46db75AE1eb2C6f9890D2AAE1A462A28"),
            "euler_earn_vault_lens": _cs("0x1d32D8A73208EE2A7fcE0BDE194c138fa7fa9b93"),
        },
        "perspectives": {
            "governed": _cs("0xb5B6AD9d08a2A6556C20AFD1D15796DEF2617e8F"),
            "evk_factory": _cs("0xC194A7A86592C712BC155979A233B3d6F00e604a"),
            "ungoverned_0x": _cs("0xFAea47832Fd23d4BB3E3208061b76E765bAa8dBA"),
            "ungoverned_nzx": _cs("0x0015d2177BF1B05648A9C39369706c8938822cbF"),
            "euler_earn_factory": _cs("0xab72CAbDAF63b5559c358A212720119EB5108E7e"),
            "euler_earn_governed": _cs("0x8bf670A110f267307a421E5C9754d0F1E9903A66"),
            "edge_factory": _cs("0x9bA5a13C6C11480F9B55E5065591E31e28379B6b"),
            "escrowed_collateral": _cs("0x68e59139e687a45939a7504023C43d3157D28EA5"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x6A72160963a562f21387B166aF31a92D154106fb"),
            "euler_swap_v1_implementation": _cs(
                "0xDFfaC13fC142Fc1d8E55226dB9c98f4b66371a3c"
            ),
            "euler_swap_v1_periphery": _cs(
                "0xAF596563109C753b9c5e73DD596DD4bB247964cA"
            ),
            "euler_swap_v2_factory": _cs("0xb0b53c1A8046D92027B69D9f6D9C7cFC0f363933"),
            "euler_swap_v2_implementation": _cs(
                "0x32Da74f7bC1988c1c39adB561b6e9D2a6F33D404"
            ),
            "euler_swap_v2_periphery": _cs(
                "0xD356C065777871B37Cb0D3C7761b8820c832BC57"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0xb7F14f649770fB7784A02A94946D14E80f79d660"
            ),
            "euler_swap_v2_registry": _cs("0xd3ee91128294Ca8231260891BEC6Da7d258De7B6"),
        },
        "periphery": {
            "swapper": _cs("0x9817C2CB138593639ae7C124893A1C1F75657B42"),
            "swap_verifier": _cs("0xD5115592F042a120cf94B506b23cac81994f677B"),
            "euler_earn_public_allocator": _cs(
                "0x4873ff8a70aA92443321Edb34a48f6aBfA7feB96"
            ),
            "external_vault_registry": _cs(
                "0xCe790A1800a54Ff2c558E2de0aaaA72243B4eF6c"
            ),
            "fee_flow_controller": _cs("0x9128754f3951a819528d110f3a92a2586D352463"),
            "fee_flow_controller_util": _cs(
                "0xb4b59CBD3052bA035540a42C049E7E305198534e"
            ),
            "irm_registry": _cs("0x71c4b9225Baaff7544a5bB29C9131365aD16Baa0"),
            "oracle_adapter_registry": _cs(
                "0xE5441e9F93A9068E9a3085A2CbD33E44De56F3e3"
            ),
            "oracle_router_factory": _cs("0x0512F7cbc4Fd9d8BC47FfFa3aA0372bA2375158E"),
        },
        "external_vault_registry": _cs("0xCe790A1800a54Ff2c558E2de0aaaA72243B4eF6c"),
    },
    59144: {
        "network": "linea",
        "status": "production",
        "evc": _cs("0xd8CeCEe9A04eA3d941a959F68fb4486f23271d09"),
        "evault_factory": _cs("0x84711986Fd3BF0bFe4a8e6d7f4E22E67f7f27F04"),
        "evault_implementation": _cs("0x58270C41552Bb2bef3Dc4e103b6f0c226032f007"),
        "euler_earn_factory": _cs("0x377879A039343FEc7564e54616e519328951DA6D"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x91868601df03ED8E134EaAaB5E06F7183CC8383f"),
        "balance_tracker": _cs("0xB9E491A3BB9d4B155d31a9cA6B9dE245CA16AAe6"),
        "sequence_registry": _cs("0xcB1bB0A8A7ddeb09983dC1e7F880DCEdc39362BA"),
        "lenses": {
            "vault_lens": _cs("0xa3D09CE7AE825f42bd271ef762b8FB038d0A2A4c"),
            "account_lens": _cs("0xdeB31DCfDe72abf31b571AfB022840dCB5D73FCf"),
            "utils_lens": _cs("0xC51a76839483A302d5E3f6b6AA02B75552632D96"),
            "oracle_lens": _cs("0x6443BF12Cf57DD5ad8af781F6518b0417212A3f8"),
            "irm_lens": _cs("0x294F6f07752Afb3470c5c2B86271C43BB3Df6284"),
            "euler_earn_vault_lens": _cs("0xF8074bbcC6e9c04EB6d3Fc69A5D502Ca774f663C"),
        },
        "perspectives": {
            "governed": _cs("0x74f9fD22aA0Dd5Bbf6006a4c9818248eb476C50A"),
            "evk_factory": _cs("0x832ca1e2FCBedf717b9C71C00Dd26805e3bE4270"),
            "ungoverned_0x": _cs("0xA3B087CC842749e2dC251DE7Ea1967a936C5335a"),
            "ungoverned_nzx": _cs("0x246667c6f8119E64b5d88cC963Ef9d4391C77C81"),
            "euler_earn_factory": _cs("0xC19CeA1886Bc1876A85572bE4041082808936B26"),
            "euler_earn_governed": _cs("0xb42a9DD67bD6b48940A862C0f0c8a6C5DD26582f"),
            "edge_factory": _cs("0xdcE47c28B8B34E0370b1DAe8067B8b2b9D24E3df"),
            "escrowed_collateral": _cs("0xc8d904FE94b65612AED5A73203C0eF8f3A0308C0"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x970B065B572CC0118535Ad1101663CDBE7Db1e21"),
            "euler_swap_v1_implementation": _cs(
                "0x2b07caff83C15c5a70C4C0867DFE7A0BE01025B0"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x0de305aB93902914909951A00079ea1df3FD98eA"
            ),
            "euler_swap_v2_factory": _cs("0xB0cc1D8e6fAc157c76d2c08B7D55Eca1573BcBDF"),
            "euler_swap_v2_implementation": _cs(
                "0x476A2ad4a7c5Ac4DF1CaA429Cb70db865A160c11"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x57729d78650cA751C9dB41f2536cA86da0032351"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0xe3ac3685D607308D4b4e26546EaDf675c37dd3dE"
            ),
            "euler_swap_v2_registry": _cs("0xEA3050E8A25f56AD0dbc90C3dCf016d8f5EfFE25"),
        },
        "periphery": {
            "swapper": _cs("0x6dE68A54105451FE9e88d44659a32291dC3959F9"),
            "swap_verifier": _cs("0x9e1D192f39489f7230Fc71aB89151a8c5A031cF0"),
            "euler_earn_public_allocator": _cs(
                "0x4148f90e03facFF8D2d5EFb475E36F94b4Ab4994"
            ),
            "external_vault_registry": _cs(
                "0x28aF9ba9152832A5B22f51510556801baDa96bBC"
            ),
            "fee_flow_controller": _cs("0xbF939812A673CB088f466d610c4b120b13eA5fAB"),
            "fee_flow_controller_util": _cs(
                "0x79af541a66DDe9b177e76839344Ea9DC2ff746aB"
            ),
            "irm_registry": _cs("0xe47732e6BAB2ae02D35879C061ac1751e7BE7aF9"),
            "oracle_adapter_registry": _cs(
                "0x5f81DdA3f9155c31f552ABC3eb4B47676ba09680"
            ),
            "oracle_router_factory": _cs("0xf0125F638c7134e6997e4F825b78c324CcF289aF"),
        },
        "external_vault_registry": _cs("0x28aF9ba9152832A5B22f51510556801baDa96bBC"),
    },
    999: {
        "network": "hyperEVM",
        "status": "production",
        "evc": _cs("0xceAA7cdCD7dDBee8601127a9Abb17A974d613db4"),
        "evault_factory": _cs("0xcF5552580fD364cdBBFcB5Ae345f75674c59273A"),
        "evault_implementation": _cs("0x05de079A28386135E048369cdf0Bc4D326d5EBDF"),
        "euler_earn_factory": _cs("0x587DD8285c01526769aB4803e4F02433ddbBc00E"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x43144f09896F8759DE2ec6D777391B9F05A51128"),
        "balance_tracker": _cs("0x05d14f4eDFA7Cbfb90711C2EC5505bcbd49b9cD2"),
        "sequence_registry": _cs("0x47618E4CBDcFBf5f21D6594A7e3a4f4683719994"),
        "lenses": {
            "vault_lens": _cs("0x34B90aeCBe2d0b1Bb337799CF0AA9939E1F39c1B"),
            "account_lens": _cs("0x66EefD479DD08B7f8B447A703bf76C4b96C42A42"),
            "utils_lens": _cs("0x920f7464b2200128673C999f4357Bd3399A9B37a"),
            "oracle_lens": _cs("0xb65A755dBE9C493dcC3EEC3aaDeb211888C1a8C5"),
            "irm_lens": _cs("0x2E79A4A15EEAd542cFe663d081D108D9cfff6D6C"),
            "euler_earn_vault_lens": _cs("0x2b76970adEAB958956975895a9F1888Ea6E4Ac4A"),
        },
        "perspectives": {
            "governed": _cs("0x4936Cd82936b6862fDD66CC8c36e1828127a6b57"),
            "evk_factory": _cs("0x7bd1DADB012651606cE70210c9c4d4c94e2480a3"),
            "ungoverned_0x": _cs("0xb2b6c3Fc174dC99dF693876740df4939f465bb9E"),
            "ungoverned_nzx": _cs("0xdf8E8Afc43AF8F2Be5CFDde0f044454DF4F0E633"),
            "euler_earn_factory": _cs("0x455Dcb38c4969f35F698115544eA4108392c79ad"),
            "euler_earn_governed": _cs("0x7b27dED9344D9c66FeAF58D151b52d1359aeA807"),
            "edge_factory": _cs("0xd15E7cD7875C77E4fA448F72476A93D409dbc033"),
            "escrowed_collateral": _cs("0xaDaDF50246512dBA23889A1eC44611B191dfF6Fc"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x0000000000000000000000000000000000000000"),
            "euler_swap_v1_implementation": _cs(
                "0x0000000000000000000000000000000000000000"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x0000000000000000000000000000000000000000"
            ),
            "euler_swap_v2_factory": _cs("0xFbF2a49CB0cc50F4ccd4eAc826eF1A76D99D29Eb"),
            "euler_swap_v2_implementation": _cs(
                "0xC00F0B7d7B4F7cA3d3f79f3892069f41C142dB84"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x61aFC386b47a11F8721b67Eb1607cFBd9ccE48B1"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x434b1072d96ea24967CDe289D3d4d81d2BAD4F30"
            ),
            "euler_swap_v2_registry": _cs("0x7E1Efb6A2009A1FDaDee1c5d6615260AD70c14Fb"),
        },
        "periphery": {
            "swapper": _cs("0xdb45C54E8100073DFB87d3D51F917A14fa565Fb0"),
            "swap_verifier": _cs("0x2F6e6Ea234a4dEcA25249252cd21D258Bb1651b8"),
            "euler_earn_public_allocator": _cs(
                "0xc00ae658ce425Bb668A5Ed96c8ECa9C988706939"
            ),
            "external_vault_registry": _cs(
                "0xe09af00Dad8f1d2F056f08Ea1059aa6cA6397FEE"
            ),
            "fee_flow_controller": _cs("0x0000000000000000000000000000000000000000"),
            "fee_flow_controller_util": _cs(
                "0x0000000000000000000000000000000000000000"
            ),
            "irm_registry": _cs("0x52930DC1b386348E9be3C9260659Dd910384A49d"),
            "oracle_adapter_registry": _cs(
                "0x66390e34511DA5DbFeD572Cc5B1337Fe57AD02E7"
            ),
            "oracle_router_factory": _cs("0x1CefA54ebBCb6c9Aa7347196B03364aFe9A89f7e"),
        },
        "external_vault_registry": _cs("0xe09af00Dad8f1d2F056f08Ea1059aa6cA6397FEE"),
    },
    9745: {
        "network": "plasma",
        "status": "production",
        "evc": _cs("0x7bdbd0A7114aA42CA957F292145F6a931a345583"),
        "evault_factory": _cs("0x42388213C6F56D7E1477632b58Ae6Bba9adeEeA3"),
        "evault_implementation": _cs("0x8346BeBaA0789Eb92CFfCC07033b8bF9f3eFdcAB"),
        "euler_earn_factory": _cs("0xA3843A73e6a9F81309B931237Ca4759B3B02ff0E"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x593Ab8A0182f752c6f1af52CA2A0E8B9F868f64A"),
        "balance_tracker": _cs("0x6e6e1e4FB3Ee6C074f10d3f80E0d3541accf7c2b"),
        "sequence_registry": _cs("0x3cf6e4c11333b30f0D0CEAe6B78f53a660df357c"),
        "lenses": {
            "vault_lens": _cs("0x167aE35aC63b2662bc8c67248321c779AbeAD195"),
            "account_lens": _cs("0x89990c6AAbbE9327a4EbD454CdCbE59b0aC8b886"),
            "utils_lens": _cs("0x60d1966DB195934459b9d36470314644041FF56A"),
            "oracle_lens": _cs("0x8120916856e8c021edDb86bce77e4d0875da0694"),
            "irm_lens": _cs("0xBd3840ec2A74ff4d0D97374BBE3a89ae72491255"),
            "euler_earn_vault_lens": _cs("0x984F25135BEc8fCabA26A6005c1632BC0DCcFd7C"),
        },
        "perspectives": {
            "governed": _cs("0xBD62C2Db0E21E4B9Ee81701F130417B8400ec854"),
            "evk_factory": _cs("0xAEA0DE17C8B1BE60B2949B7F17482EBe681F93DF"),
            "ungoverned_0x": _cs("0x586471dAe0AEe957e053399347b23eFD0a69eD74"),
            "ungoverned_nzx": _cs("0x23Fd93a4AC2A0d87785Acd925BcfebA550006327"),
            "euler_earn_factory": _cs("0xE5f18b8E25E60F74d627FdDc6805Ec3cBE853573"),
            "euler_earn_governed": _cs("0xAA8b9729a047568CB0614165509229A86e345Be1"),
            "edge_factory": _cs("0xe0DdB3b94104757865Fe5165a59AE55767573Ea9"),
            "escrowed_collateral": _cs("0xA269918388E3824b5B9316C20c8D5f9D558b73CA"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0xD7aA03104c2CCaC58acB00CbE90865FA64BbE77D"),
            "euler_swap_v1_implementation": _cs(
                "0x7F6ff62e7ECED715a2f4E5Ebe14eC9d32a44EFDc"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x1472ebB000190275B5e28733e45a2614F1C3F41C"
            ),
            "euler_swap_v2_factory": _cs("0x29FAFDbf952e7b5c0A6Cd26957829334d54E872A"),
            "euler_swap_v2_implementation": _cs(
                "0xdAdBb7a06638e3345A341002e956324A46d1c28c"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x741Fc7A904c9F810cbc4a21DE7D07B51B5Da853C"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x8ec298B473D17e04F819453C72747c3d4d6B7848"
            ),
            "euler_swap_v2_registry": _cs("0x8D6A81Ec8f5680849dCFBa47c710Dd9DA02aDaea"),
        },
        "periphery": {
            "swapper": _cs("0x8B8Ce23C9BbB2c26BA322Ec1Aa266BAF6226ccc0"),
            "swap_verifier": _cs("0xcB80Af483ecA49e5ca7d4DBa2F24D01E9f0be289"),
            "euler_earn_public_allocator": _cs(
                "0x667aD135188d95a32A4E743Aebe5a5b503cb9038"
            ),
            "external_vault_registry": _cs(
                "0xc92a47A62322914472eaCe515Cd1c5DAC31FCa37"
            ),
            "fee_flow_controller": _cs("0xBCc714F3ce3F56aB4A85a10d593cF9C93ED6ED9e"),
            "fee_flow_controller_util": _cs(
                "0x730a68b4dfD36b804a6466678059B838632f1CF1"
            ),
            "irm_registry": _cs("0xe5F7889cA0cF16926eb73a523ea364B8539aaf87"),
            "oracle_adapter_registry": _cs(
                "0xe7581A54eBEcC02B42a9c0b4044abC9bb75A502D"
            ),
            "oracle_router_factory": _cs("0x7e539159a06CFe0A9f855d22dD82aD95eDf8C2F1"),
        },
        "external_vault_registry": _cs("0xc92a47A62322914472eaCe515Cd1c5DAC31FCa37"),
    },
    143: {
        "network": "monad",
        "status": "production",
        "evc": _cs("0x7a9324E8f270413fa2E458f5831226d99C7477CD"),
        "evault_factory": _cs("0xba4Dd672062dE8FeeDb665DD4410658864483f1E"),
        "evault_implementation": _cs("0xef17750D3a162E28a302E266c474ff8989d60ECD"),
        "euler_earn_factory": _cs("0xF463d4Acb650cc6C4E1D6cD4D0d1b0cb224094cF"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x94A2d1d175F1d828935a374091e2009CF1cED858"),
        "balance_tracker": _cs("0xa231DccE58EA5A43E69EF351D89ea4212Ec0f30b"),
        "sequence_registry": _cs("0x39F81037f20AC6068CbCd30f748094c58bfE7d7b"),
        "lenses": {
            "vault_lens": _cs("0x13958d27dbCEce91aafA13C4fD4772efb1C23e15"),
            "account_lens": _cs("0x960D481229f70c3c1CBCD3fA2d223f55Db9f36Ee"),
            "utils_lens": _cs("0xA9eCab3Dc8cA2855387D12E9E889F1EAfd61f91B"),
            "oracle_lens": _cs("0x0dE96d33afF54F3e8750567F6038A05c6D3aAa96"),
            "irm_lens": _cs("0x615e1dAb9cF1Ad2b065B0c85720258c8d6236004"),
            "euler_earn_vault_lens": _cs("0x78f40a9822d170D7bC275986Dc2a4eF02C972367"),
        },
        "perspectives": {
            "governed": _cs("0x8707B105567661E7c6B41cDd1b3EC7D784e5FA50"),
            "evk_factory": _cs("0x9266C8c71fDA44EcC7Df2A14E12C6E1aA9C96Ca7"),
            "ungoverned_0x": _cs("0x47B7b629409117e5C99D9F161E47Ff304cF520f6"),
            "ungoverned_nzx": _cs("0x36951cC4AC6f8Ec5E01787a95689b2C3466E6538"),
            "euler_earn_factory": _cs("0x0490013112B6beb8545E6776cc67D8A40023690a"),
            "euler_earn_governed": _cs("0xe4A695d715732db3d694E30EC57b1acc8cC4368b"),
            "edge_factory": _cs("0xcFcb87Eb3E796B35C9678feD440B2a857af94ed9"),
            "escrowed_collateral": _cs("0xf3e1Dd13C448A7E1a6e19ba8A7f29f45C1E93AaB"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x34f8F028C6a446A464c10a135F44Fc6fB2CEe1A9"),
            "euler_swap_v1_implementation": _cs(
                "0xBFD5C7bb1C208FEc761284Af7dB6fF1F4314372c"
            ),
            "euler_swap_v1_periphery": _cs(
                "0xd1F69cf959c1a3AAe7BEE5ec677222d259585B27"
            ),
            "euler_swap_v2_factory": _cs("0x0000000000000000000000000000000000000000"),
            "euler_swap_v2_implementation": _cs(
                "0x0000000000000000000000000000000000000000"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x0000000000000000000000000000000000000000"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x0000000000000000000000000000000000000000"
            ),
            "euler_swap_v2_registry": _cs("0x0000000000000000000000000000000000000000"),
        },
        "periphery": {
            "swapper": _cs("0x41B8Ec27c640DbD0299A0083fAc8fE0099648bdB"),
            "swap_verifier": _cs("0x392812023A2Ef4F20DE5AA9f7b7e2F02E9692Ba7"),
            "euler_earn_public_allocator": _cs(
                "0x65A66F24a25E8CF651C9e31D296623298C80F742"
            ),
            "external_vault_registry": _cs(
                "0x62e9d884cbE9a6B59a6014c9751C06551B83943E"
            ),
            "fee_flow_controller": _cs("0x9527062A472666410DC7193A966709105dF2f147"),
            "fee_flow_controller_util": _cs(
                "0x260fA2473c23F362344d733B56e64864a9Efe92c"
            ),
            "irm_registry": _cs("0xB402699Ab5B255b68b4A4cdD3E171a67A9124FC7"),
            "oracle_adapter_registry": _cs(
                "0xCC5EDE0Ea39f2F8c80C461B7e954FB4256773AFa"
            ),
            "oracle_router_factory": _cs("0xdDA3cBC18e90606A83FBae6F798991af06dFA902"),
        },
        "external_vault_registry": _cs("0x62e9d884cbE9a6B59a6014c9751C06551B83943E"),
    },
    2818: {
        "network": "morph",
        "status": "production",
        "evc": _cs("0xC7c31B1E7Cac36478C62f876F357b95d9cAd9817"),
        "evault_factory": _cs("0x1a1bdF62Fe7170c652f03fF153977C26fBe7b2E1"),
        "evault_implementation": _cs("0xC98f21E7b1F12ab225386226D022a67E65D8B0cE"),
        "euler_earn_factory": _cs("0x4A6727aA2d1979C0366751c7a81615500f0186E4"),
        "permit2": _cs("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "protocol_config": _cs("0x235597e32131A6681592Ac276c967a5b8c89dCb3"),
        "balance_tracker": _cs("0x7bF00B8638d0eBE917C82B286Fc07E86988F3A1C"),
        "sequence_registry": _cs("0x433438DD80353fB1893253348b7b14f821b4cCFB"),
        "lenses": {
            "vault_lens": _cs("0x995ac28051CECfe4Be4750a2b8BFA7c72FBFf2ae"),
            "account_lens": _cs("0x9721c0ca5795000D2cD9B084F52612c9c7B1e7a6"),
            "utils_lens": _cs("0xB6468fda36fbD90Ae519bb9966c5Ff72C7577f1E"),
            "oracle_lens": _cs("0x3756420C286Bc07ef00Ca8b4Db7854C81F385d22"),
            "irm_lens": _cs("0x1D021C08c1De505348cFD6DF74861A854E3E2eC0"),
            "euler_earn_vault_lens": _cs("0x8D1574A8a07AdE966c232a04308b6e89f9FDC13a"),
        },
        "perspectives": {
            "governed": _cs("0xCb10109814771F5525e958CB938053712dEE40e8"),
            "evk_factory": _cs("0xfB4bf64F87be32668e81D77F728B0001cfE430De"),
            "ungoverned_0x": _cs("0x3ce733bCbb42e5468F0eC18b20ee035027167c8B"),
            "ungoverned_nzx": _cs("0x28E1CBb51C2eBaC2c13334f1983772ece465CF4D"),
            "euler_earn_factory": _cs("0x682D7941910dC47F6954EF9b4BEf6190b300AA62"),
            "euler_earn_governed": _cs("0xEB50B69Eea78396BC926b321B1A36EFF04658Db8"),
            "edge_factory": _cs("0x6C35E81B8419835a3E3940f3Db0C10c14F8b5181"),
            "escrowed_collateral": _cs("0x580d1a16525884BF994B00705b276AD0977BB9D9"),
        },
        "swaps": {
            "euler_swap_v1_factory": _cs("0x0000000000000000000000000000000000000000"),
            "euler_swap_v1_implementation": _cs(
                "0x0000000000000000000000000000000000000000"
            ),
            "euler_swap_v1_periphery": _cs(
                "0x0000000000000000000000000000000000000000"
            ),
            "euler_swap_v2_factory": _cs("0x5965230117acd46E88CA13e7D44E33cD166c1E44"),
            "euler_swap_v2_implementation": _cs(
                "0xDA4d6a51cdF37d93C84071192611B0544B4454d8"
            ),
            "euler_swap_v2_periphery": _cs(
                "0x9C0689e034D5bfDCc190F062b70c364c4764EF35"
            ),
            "euler_swap_v2_protocol_fee_config": _cs(
                "0x2348C3967F61b235501Ea74eE8e95F84D752dDeA"
            ),
            "euler_swap_v2_registry": _cs("0xce4Ef4F4c590FE00c213e1A458743d12e7301268"),
        },
        "periphery": {
            "swapper": _cs("0x57c1BEB600b79D1261be981ca52D801d44CEC8BB"),
            "swap_verifier": _cs("0xD38D4F982Ca00365e349dC128C833289be4774c9"),
            "euler_earn_public_allocator": _cs(
                "0x776043C581c7b30164b92358be317b16489C510b"
            ),
            "external_vault_registry": _cs(
                "0x8B039605Bd79454E960a639083696fFFc97996E3"
            ),
            "fee_flow_controller": _cs("0x9DF2ea8c76439B5c37a2507578e4b3E9a137eC17"),
            "fee_flow_controller_util": _cs(
                "0x0000000000000000000000000000000000000000"
            ),
            "irm_registry": _cs("0xCE76B0E4ec11393aBdF6a46e38cD2EEa57Ec9d5f"),
            "oracle_adapter_registry": _cs(
                "0x290e12cc83D84DA4ec3F4DB1Ebc366E015af517D"
            ),
            "oracle_router_factory": _cs("0x1e80949625ab6b09a5749f00a63d48887ba3B696"),
        },
        "external_vault_registry": _cs("0x8B039605Bd79454E960a639083696fFFc97996E3"),
    },
}
