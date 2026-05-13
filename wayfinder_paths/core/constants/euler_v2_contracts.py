from __future__ import annotations

from typing import Any

from eth_utils import to_checksum_address

# Euler Vault Kit (EVK / eVault) deployments, sourced from Euler's official registry:
# https://raw.githubusercontent.com/euler-xyz/euler-interfaces/master/EulerChains.json
#
# Notes:
# - The "vault" address is the market itself (ERC-4626 share token).
# - EVC (Ethereum Vault Connector) is the recommended entrypoint for state-changing ops.
EULER_V2_BY_CHAIN: dict[int, dict[str, Any]] = {
    1: {
        "network": "ethereum",
        "evc": to_checksum_address("0x0C9a3dd6b8F28529d72d7f9cE918D493519EE383"),
        "evault_factory": to_checksum_address(
            "0x29a56a1b8214D9Cf7c5561811750D5cBDb45CC8e"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0xc3c45633E45041BF3BE841f89d2cb51E2F657403"
            ),
            "account_lens": to_checksum_address(
                "0xA60c4257c809353039A71527dfe701B577e34bc7"
            ),
            "utils_lens": to_checksum_address(
                "0x1B6E0b25Fef3131f5F461B000cC69d2611Af2D95"
            ),
            "oracle_lens": to_checksum_address(
                "0x30E6dFB84782A31d561536f64F47231451F7b48A"
            ),
            "irm_lens": to_checksum_address(
                "0x57B1BB683b109eB0F1e6d9043067C86F0C6c52C1"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0xC0121817FF224a018840e4D15a864747d36e6Eb2"
            ),
            "evk_factory": to_checksum_address(
                "0xB30f23bc5F93F097B3A699f71B0b1718Fc82e182"
            ),
            "ungoverned_0x": to_checksum_address(
                "0xb50a07C2B0F128Faa065bD18Ea2091F5da5e7FbF"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0x600bBe1D0759F380Fea72B2e9B2B6DCb4A21B507"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0xB3b30ffb54082CB861B17DfBE459370d1Cc219AC"
        ),
    },
    56: {
        "network": "bsc",
        "evc": to_checksum_address("0xb2E5a73CeE08593d1a076a2AE7A6e02925a640ea"),
        "evault_factory": to_checksum_address(
            "0x7F53E2755eB3c43824E162F7F6F087832B9C9Df6"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0xC10801e635B1683de5D22c298f249a489319ea59"
            ),
            "account_lens": to_checksum_address(
                "0x9578D17d2e1AA70EA6f9eC8A39967bfD1c6F6217"
            ),
            "utils_lens": to_checksum_address(
                "0x537F650a4fD350061F3c07F704F744F2c772dAC5"
            ),
            "oracle_lens": to_checksum_address(
                "0x301a83Cf9ffbDe64DbBD7F7988e900704cbCa2cb"
            ),
            "irm_lens": to_checksum_address(
                "0x56Fb9F68354aF7DAE4E3b2067B53C498ECD053C6"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0x775231E5da4F548555eeE633ebf7355a83A0FC03"
            ),
            "evk_factory": to_checksum_address(
                "0x9d928D359646dC4249A8d57259d87673F118Ec85"
            ),
            "ungoverned_0x": to_checksum_address(
                "0xea19a15182A78e8fFF080F79C769FBB590f4D3E9"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0x32581e478819f24434baC9041542770026cE32A7"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0x74171139d712AE64faA8cEFA524e13fd52826c1b"
        ),
    },
    130: {
        "network": "unichain",
        "evc": to_checksum_address("0x2A1176964F5D7caE5406B627Bf6166664FE83c60"),
        "evault_factory": to_checksum_address(
            "0xbAd8b5BDFB2bcbcd78Cc9f1573D3Aad6E865e752"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0x46D18cB4370BAC8734C319EF965042C1DC7091B7"
            ),
            "account_lens": to_checksum_address(
                "0xa06b923a85d96c62205fA007435E375e9d0Ce31f"
            ),
            "utils_lens": to_checksum_address(
                "0xc05e44362868A054Cf5D875D259029E9A26751f4"
            ),
            "oracle_lens": to_checksum_address(
                "0x30100D82EE8Fd7dE7a9762Dce7f08055fdADb9Be"
            ),
            "irm_lens": to_checksum_address(
                "0x227cc7C2DA74bE56A24Df0f4cDFFb7F227fc86f8"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0x44d781D9f61649fACeeEC919c71C8537531df027"
            ),
            "evk_factory": to_checksum_address(
                "0x5A2164C500f4FD26AB037d97A3ed5d0774446c6B"
            ),
            "ungoverned_0x": to_checksum_address(
                "0xeEF6CF66abbD88fe97BeE236aac21285158f3a3A"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0xcAb8bBe881a13A513770746AF15F7cC884843734"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0xC0a8dFA92CB9FF9F503803D3bAE2CF19E9c15411"
        ),
    },
    143: {
        "network": "monad",
        "evc": to_checksum_address("0x7a9324E8f270413fa2E458f5831226d99C7477CD"),
        "evault_factory": to_checksum_address(
            "0xba4Dd672062dE8FeeDb665DD4410658864483f1E"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0x15d1Cc54fB3f7C0498fc991a23d8Dc00DF3c32A0"
            ),
            "account_lens": to_checksum_address(
                "0x960D481229f70c3c1CBCD3fA2d223f55Db9f36Ee"
            ),
            "utils_lens": to_checksum_address(
                "0x3043f84052981c098c74A1d02bbf125D0cB20e50"
            ),
            "oracle_lens": to_checksum_address(
                "0x0dE96d33afF54F3e8750567F6038A05c6D3aAa96"
            ),
            "irm_lens": to_checksum_address(
                "0x615e1dAb9cF1Ad2b065B0c85720258c8d6236004"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0x8707B105567661E7c6B41cDd1b3EC7D784e5FA50"
            ),
            "evk_factory": to_checksum_address(
                "0x9266C8c71fDA44EcC7Df2A14E12C6E1aA9C96Ca7"
            ),
            "ungoverned_0x": to_checksum_address(
                "0x47B7b629409117e5C99D9F161E47Ff304cF520f6"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0x36951cC4AC6f8Ec5E01787a95689b2C3466E6538"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0x62e9d884cbE9a6B59a6014c9751C06551B83943E"
        ),
    },
    146: {
        "network": "sonic",
        "evc": to_checksum_address("0x4860C903f6Ad709c3eDA46D3D502943f184D4315"),
        "evault_factory": to_checksum_address(
            "0xF075cC8660B51D0b8a4474e3f47eDAC5fA034cFB"
        ),
        "permit2": to_checksum_address("0xB952578f3520EE8Ea45b7914994dcf4702cEe578"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0x3bd2B8f04C9C04c0322127ccF683C6B288bD27B8"
            ),
            "account_lens": to_checksum_address(
                "0x99Cf844584BBFa12E6b76a9FD3C08C2Dd99F87C4"
            ),
            "utils_lens": to_checksum_address(
                "0x8d9427171f0092093c68315045dc1F6030d1aF51"
            ),
            "oracle_lens": to_checksum_address(
                "0x6Ed14a85CEF0048C57Cb13c2Eb5435eA723e8123"
            ),
            "irm_lens": to_checksum_address(
                "0x59a3C9F50d1357b06Eda2f40e2E57DB03988b05F"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0x93478469b049e75B8d20b6d2c5A8da84E35f14D0"
            ),
            "evk_factory": to_checksum_address(
                "0x69D2403d9a0715CDc89AcB015Ec2AfB200C4f6BD"
            ),
            "ungoverned_0x": to_checksum_address(
                "0x770500Ee92d2C395Aa39f2C573A08D78D5FF8090"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0x2a75a1D4e4bba15e74693141f8D75f206BFa2967"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0x650737Bf472588A04530494189c3c30eaF5f6C50"
        ),
    },
    239: {
        "network": "tac",
        "evc": to_checksum_address("0x01F594c66A5561b90Bc782dD0297f294cD668b64"),
        "evault_factory": to_checksum_address(
            "0x2b21621b8Ef1406699a99071ce04ec14cCd50677"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0x5218E5970e480c9cd904929Ea197af8E9c8E5CE8"
            ),
            "account_lens": to_checksum_address(
                "0x8A3b3E493733e54977B539A4E475Bf16463ecBD6"
            ),
            "utils_lens": to_checksum_address(
                "0x78Aa08c6371980f5C244F76c6e4a9958fc4f5b4a"
            ),
            "oracle_lens": to_checksum_address(
                "0xB7b2530A8a545504d35F7502E0bf9Fba59F772D6"
            ),
            "irm_lens": to_checksum_address(
                "0x30DD8F6A46db75AE1eb2C6f9890D2AAE1A462A28"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0xb5B6AD9d08a2A6556C20AFD1D15796DEF2617e8F"
            ),
            "evk_factory": to_checksum_address(
                "0xC194A7A86592C712BC155979A233B3d6F00e604a"
            ),
            "ungoverned_0x": to_checksum_address(
                "0xFAea47832Fd23d4BB3E3208061b76E765bAa8dBA"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0x0015d2177BF1B05648A9C39369706c8938822cbF"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0xCe790A1800a54Ff2c558E2de0aaaA72243B4eF6c"
        ),
    },
    999: {
        "network": "hyperevm",
        "evc": to_checksum_address("0xceAA7cdCD7dDBee8601127a9Abb17A974d613db4"),
        "evault_factory": to_checksum_address(
            "0xcF5552580fD364cdBBFcB5Ae345f75674c59273A"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0x0eaDDE9EfCf1540dcA8f94e813E12db55f8405a8"
            ),
            "account_lens": to_checksum_address(
                "0x66EefD479DD08B7f8B447A703bf76C4b96C42A42"
            ),
            "utils_lens": to_checksum_address(
                "0xB3EC37ebA3Ea95cb4A6A34883485b9e8fC3b67C6"
            ),
            "oracle_lens": to_checksum_address(
                "0xb65A755dBE9C493dcC3EEC3aaDeb211888C1a8C5"
            ),
            "irm_lens": to_checksum_address(
                "0x2E79A4A15EEAd542cFe663d081D108D9cfff6D6C"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0x4936Cd82936b6862fDD66CC8c36e1828127a6b57"
            ),
            "evk_factory": to_checksum_address(
                "0x7bd1DADB012651606cE70210c9c4d4c94e2480a3"
            ),
            "ungoverned_0x": to_checksum_address(
                "0xb2b6c3Fc174dC99dF693876740df4939f465bb9E"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0xdf8E8Afc43AF8F2Be5CFDde0f044454DF4F0E633"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0xe09af00Dad8f1d2F056f08Ea1059aa6cA6397FEE"
        ),
    },
    1923: {
        "network": "swell",
        "evc": to_checksum_address("0x08739CBede6E28E387685ba20e6409bD16969Cde"),
        "evault_factory": to_checksum_address(
            "0x238bF86bb451ec3CA69BB855f91BDA001aB118b9"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0xFa903304784e555226450341D7dFeEd1F60a236b"
            ),
            "account_lens": to_checksum_address(
                "0x8fE9A01F035B2C6891fD4F70f489A96dc746a08C"
            ),
            "utils_lens": to_checksum_address(
                "0xeEEbbBfA2a6DA2898754579D77E9EE597CA4af53"
            ),
            "oracle_lens": to_checksum_address(
                "0x336b1F8969557E69536E69055B4ef0fB8762b135"
            ),
            "irm_lens": to_checksum_address(
                "0x4365E4dE90d454EE1D2F6e3D09e4e30B1Ab93c44"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0xda258aB9569d0156B99943aDC4083E542F70a6f1"
            ),
            "evk_factory": to_checksum_address(
                "0x96070bE9d3dFb045c6C96D35CeCc70Aa2940c756"
            ),
            "ungoverned_0x": to_checksum_address(
                "0x96367505890EF888c0C92E19a9814fa27B461549"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0x2671dA0a4539886cDd4E40096fF1A70b45fc7289"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0x575fcBb7a9f72F8550E578f8fEed6Ac40e0b3b5C"
        ),
    },
    8453: {
        "network": "base",
        "evc": to_checksum_address("0x5301c7dD20bD945D2013b48ed0DEE3A284ca8989"),
        "evault_factory": to_checksum_address(
            "0x7F321498A801A191a93C840750ed637149dDf8D0"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0x7Bb9493381387c41bF9F26Ca47eDdF4D3d534036"
            ),
            "account_lens": to_checksum_address(
                "0xe6b05A38D6a29D2C8277fA1A8BA069F1693b780C"
            ),
            "utils_lens": to_checksum_address(
                "0x3e8a945a1C4c855359bB8d85aD8879154a8b42e7"
            ),
            "oracle_lens": to_checksum_address(
                "0x91517C3E57C7e426a0221dEFB21d0acf8231b8b6"
            ),
            "irm_lens": to_checksum_address(
                "0xc159d463E7Cdb2C4bA8D4C0C877127A1fCdf33dC"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0xafC8545c49DF2c8216305922D9753Bf60bf8c14A"
            ),
            "evk_factory": to_checksum_address(
                "0xFEA8e8a4d7ab8C517c3790E49E92ED7E1166F651"
            ),
            "ungoverned_0x": to_checksum_address(
                "0x24F2b095df7c76266fd037b847360f69eD591549"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0xFff2dA17172588629Adf5BDEF275d9AbEBbA39Bd"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0x6A60B3E561F0a7d9587F3210426FeC882224dF2d"
        ),
    },
    42161: {
        "network": "arbitrum",
        "evc": to_checksum_address("0x6302ef0F34100CDDFb5489fbcB6eE1AA95CD1066"),
        "evault_factory": to_checksum_address(
            "0x78Df1CF5bf06a7f27f2ACc580B934238C1b80D50"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0xc99FCEE6174Bc92eBe9C78690fFD5067018a8380"
            ),
            "account_lens": to_checksum_address(
                "0x90a52DDcb232e7bb003DD9258fA1235c553eC956"
            ),
            "utils_lens": to_checksum_address(
                "0xDAf44060DCe217Fd603908A49fcaa1FA900304BE"
            ),
            "oracle_lens": to_checksum_address(
                "0x5D613b4eC0efAee328f6cA47C667EA49a2eB7884"
            ),
            "irm_lens": to_checksum_address(
                "0x9ac753B76B56039e4164858f90c288AC1346EC3c"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0xc7693ceEf74Bc7c8Af703c5519F24bB5e6642643"
            ),
            "evk_factory": to_checksum_address(
                "0x03a931446F5A7e7ec1D850D8eaF95Ab68Ad9089C"
            ),
            "ungoverned_0x": to_checksum_address(
                "0x068789293D461Be145D14BfC0e270941554CAC26"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0xfbB90dce4a2aCb5425b96B7886D621DE913c816D"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0xFB13aa1d7CFe1C85826f9D5e571589B13b785A6e"
        ),
    },
    43114: {
        "network": "avalanche",
        "evc": to_checksum_address("0xddcbe30A761Edd2e19bba930A977475265F36Fa1"),
        "evault_factory": to_checksum_address(
            "0xaf4B4c18B17F6a2B32F6c398a3910bdCD7f26181"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0x1521C9DCA248ceE906943096a5B13Fc657A020C3"
            ),
            "account_lens": to_checksum_address(
                "0x08bb803D19e5E2F006C87FEe77c232Dc481cB735"
            ),
            "utils_lens": to_checksum_address(
                "0xB671675AAA5D87639072d6a2682480d445eBc3Ab"
            ),
            "oracle_lens": to_checksum_address(
                "0xC5FFCe5f0e6646D93F7E79bD71d268dFC1B7EfD7"
            ),
            "irm_lens": to_checksum_address(
                "0x8D990f217879E3C49894024f5D72431DA3Ef656C"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0x0d1ABCcBa91F074DeA11AdCc679C61326b6145AC"
            ),
            "evk_factory": to_checksum_address(
                "0x4247432b4f9c32e99ecC2Ff7bAdd98783EecFA6F"
            ),
            "ungoverned_0x": to_checksum_address(
                "0x299f86BbB552F74Be79A687c565aC52452C0a02d"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0xC2675790c775D385425D72652ded5f299Fbb2868"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0xe41338Ccac8121fb472817c58c485776E77f3Eea"
        ),
    },
    59144: {
        "network": "linea",
        "evc": to_checksum_address("0xd8CeCEe9A04eA3d941a959F68fb4486f23271d09"),
        "evault_factory": to_checksum_address(
            "0x84711986Fd3BF0bFe4a8e6d7f4E22E67f7f27F04"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0x44fc1F24b91B654360FED14520ea03F13a8a25C1"
            ),
            "account_lens": to_checksum_address(
                "0xdeB31DCfDe72abf31b571AfB022840dCB5D73FCf"
            ),
            "utils_lens": to_checksum_address(
                "0x035Ef2a6730A5Fc54f9302888Ec1A4785818c5e8"
            ),
            "oracle_lens": to_checksum_address(
                "0x6443BF12Cf57DD5ad8af781F6518b0417212A3f8"
            ),
            "irm_lens": to_checksum_address(
                "0x294F6f07752Afb3470c5c2B86271C43BB3Df6284"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0x74f9fD22aA0Dd5Bbf6006a4c9818248eb476C50A"
            ),
            "evk_factory": to_checksum_address(
                "0x832ca1e2FCBedf717b9C71C00Dd26805e3bE4270"
            ),
            "ungoverned_0x": to_checksum_address(
                "0xA3B087CC842749e2dC251DE7Ea1967a936C5335a"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0x246667c6f8119E64b5d88cC963Ef9d4391C77C81"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0x28aF9ba9152832A5B22f51510556801baDa96bBC"
        ),
    },
    9745: {
        "network": "plasma",
        "evc": to_checksum_address("0x7bdbd0A7114aA42CA957F292145F6a931a345583"),
        "evault_factory": to_checksum_address(
            "0x42388213C6F56D7E1477632b58Ae6Bba9adeEeA3"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0x62FF27a1fBE6024D2933A88D39E0FF877dB4FE0B"
            ),
            "account_lens": to_checksum_address(
                "0x89990c6AAbbE9327a4EbD454CdCbE59b0aC8b886"
            ),
            "utils_lens": to_checksum_address(
                "0xc55f6e262FE21Da068ece5D3fa015D8451bAf625"
            ),
            "oracle_lens": to_checksum_address(
                "0x8120916856e8c021edDb86bce77e4d0875da0694"
            ),
            "irm_lens": to_checksum_address(
                "0xBd3840ec2A74ff4d0D97374BBE3a89ae72491255"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0xBD62C2Db0E21E4B9Ee81701F130417B8400ec854"
            ),
            "evk_factory": to_checksum_address(
                "0xAEA0DE17C8B1BE60B2949B7F17482EBe681F93DF"
            ),
            "ungoverned_0x": to_checksum_address(
                "0x586471dAe0AEe957e053399347b23eFD0a69eD74"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0x23Fd93a4AC2A0d87785Acd925BcfebA550006327"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0xc92a47A62322914472eaCe515Cd1c5DAC31FCa37"
        ),
    },
    60808: {
        "network": "bob",
        "evc": to_checksum_address("0x59f0FeEc4fA474Ad4ffC357cC8d8595B68abE47d"),
        "evault_factory": to_checksum_address(
            "0x046a9837A61d6b6263f54F4E27EE072bA4bdC7e4"
        ),
        "permit2": to_checksum_address("0xCbe9Be2C87b24b063A21369b6AB0Aa9f149c598F"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0x2232d5B6209BBc82af29ffF888b581FC685a2a86"
            ),
            "account_lens": to_checksum_address(
                "0x41FE40e10268decF2D25c60aDf60469EE94E8771"
            ),
            "utils_lens": to_checksum_address(
                "0x02C1F7453F2968690ecCbC2191B2dd37e8A88E24"
            ),
            "oracle_lens": to_checksum_address(
                "0x428a144214D3E640634491F05b7130ba26A394f3"
            ),
            "irm_lens": to_checksum_address(
                "0x0e367C8a8ffBF4555f1B4bC1fA99CA97c253Fe33"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0xed62ebA9552dF86b5F7d995eD00C06494bBbB638"
            ),
            "evk_factory": to_checksum_address(
                "0x05B98f64A31A33666cC9D2B32046a6Ca42699823"
            ),
            "ungoverned_0x": to_checksum_address(
                "0x878343fc7AA3F3eC841D6C6A0e942B7209EF0D30"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0x6853213a8c0b66b7148B87E8D5cCfc580F60c077"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0x28029B4De813866A4F7F03AeE4445732F02B3B09"
        ),
    },
    80094: {
        "network": "berachain",
        "evc": to_checksum_address("0x45334608ECE7B2775136bC847EB92B5D332806A9"),
        "evault_factory": to_checksum_address(
            "0x5C13fb43ae9BAe8470f646ea647784534E9543AF"
        ),
        "permit2": to_checksum_address("0x000000000022D473030F116dDEE9F6B43aC78BA3"),
        "lenses": {
            "vault_lens": to_checksum_address(
                "0xA7d2a277DA8173029f052886aeA292760f234346"
            ),
            "account_lens": to_checksum_address(
                "0xfC09040C5E26aec5E55a93F6856159A0C28ffDB9"
            ),
            "utils_lens": to_checksum_address(
                "0x9e87e57DCc8B4d81C29EaEF9eC45B2A3414A5ddd"
            ),
            "oracle_lens": to_checksum_address(
                "0x0c0519BaA6c1556f9d4dc9E26073D1BF7C48DC2e"
            ),
            "irm_lens": to_checksum_address(
                "0x743678dAE1d3358965ACAae333F8BADE1952723a"
            ),
        },
        "perspectives": {
            "governed": to_checksum_address(
                "0xAE06ad3a165acA82AC4eFEcdE2D3875414C419b2"
            ),
            "evk_factory": to_checksum_address(
                "0xEE0CA74F3c60B7e1366e6d64AE2426E5177145cf"
            ),
            "ungoverned_0x": to_checksum_address(
                "0x853ea3e0942e74B65D65275b2A2F3237B83A58d8"
            ),
            "ungoverned_nzx": to_checksum_address(
                "0xE86e9B82788C1438b95346E7BF180AAf91AFC4bb"
            ),
        },
        "external_vault_registry": to_checksum_address(
            "0x73313Bc5aF05187466f42c53eaF4851816bd76CD"
        ),
    },
}
