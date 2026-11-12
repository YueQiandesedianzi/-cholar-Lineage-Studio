# -*- coding: utf-8 -*-
"""把聚酰亚胺建模综述的语料灌入 ReviewGit MVP（SQLite）。
通过 app.test_client() 走真实 API：导入文献 → 创建模板 → 提取证据卡片 → 核验 → 建立 8 节框架 → 卡片挂载。
语料摘要来自文献检索结果（真实文本，仅作演示用）。
"""
import os
import sys

base = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, base)

db = os.environ.get("REVIEWGIT_DB", os.path.join(base, "reviewgit.db"))
if os.path.exists(db):
    os.remove(db)
os.environ["REVIEWGIT_DB"] = db

import app
c = app.app.test_client()

PAPERS = [
 {"title": "Hierarchical multiscale analysis of polyimide films by molecular dynamics simulation",
  "authors": "Huanyu Lei; Shengli Qi; Dezhen Wu", "year": "2019",
  "doi": "10.1016/j.polymer.2019.121645",
  "abstract": "The molecular dynamics simulations are adopted to investigate the thermo-mechanical properties of PMDA/BIA, PMDA/PDA, PMDA/ODA, BPDA/BIA, BPDA/PDA and BPDA/ODA polyimide films in hierarchical multiscale level. The results indicate that the properties follow the order of BIA > PDA > ODA. At a small scale, the hydrogen bonding in the BPDA/BIA polyimide strengthens the chain interactions and results in a coiling-shaped chain conformation. At a large scale, the torsion angle distribution changes apparently before and after stretching, which increases the d-spacing and elongates the entire molecular chain simultaneously, serving as the main cause of polyimide tensile deformation."},
 {"title": "Extraction of Mechanical Parameters via Molecular Dynamics Simulation: Application to Polyimides",
  "authors": "Philipp Rosenauer; Christoph Kratzer; Silvia Larisegger; Stefan Radl", "year": "2024",
  "doi": "10.3390/polym16060813",
  "abstract": "Here, we perform all-atom molecular dynamics simulations for such materials to assess their predictive capability with respect to their mechanical properties. We demonstrate that the OPLS-AA force field can be used to successfully describe an often-used polyimide (Kapton) with respect to its Young's modulus and Poisson's ratio. Our continuous deformation mode simulations almost perfectly replicate the results from real-world experimental data."},
 {"title": "Simulating local mobility and mechanical properties of thermostable polyimides with different dianhydride fragments",
  "authors": "Victor M. Nazarychev; Alexey Yu Dobrovskiy; Sergey V. Larin; et al.", "year": "2017",
  "doi": "10.1002/polb.24550",
  "abstract": "The dynamic and mechanical properties of the three thermoplastic polyimides have been simulated using the atomistic molecular dynamics technique. It has been established that the increase of the glass-transition temperature (Tg) is caused by the slowing down of the phthalimide rings relaxation. The increase of the dynamic fragility leads to the increase of the strain-hardening modulus."},
 {"title": "Molecular dynamics study on the effect of polyimide molecular structure on molecular segment movement behavior",
  "authors": "Tianqi Hou; Chuan He; Ying Lin; et al.", "year": "2025",
  "doi": "10.1016/j.mtcomm.2025.112804",
  "abstract": "The molecular structure of polyimide fundamentally influences its various properties. Four PI models with representative molecular configurations were established and examined using molecular dynamics methods. The results further demonstrate that the molecular structure is the primary reason for the performance differences observed in PI."},
 {"title": "Influence of the electrostatic interactions on thermophysical properties of polyimides",
  "authors": "Stanislav G. Falkovich; Sergey V. Lyulin; et al.", "year": "2014",
  "doi": "10.1002/polb.23460",
  "abstract": "Two thermoplastic polyimides, ULTEM and EXTEM, were simulated with small difference in chemical structures of monomer units. It is shown that such a small modification results in a drastic difference of the thermal properties. Large partial charges of the sulphone group in the EXTEM lead to strong dipole-dipole interactions and correspondingly to an elevated glass transition temperature."},
 {"title": "Application of Molecular Simulation in the Study of Polyimide",
  "authors": "Huan-yu Lei; Guo-feng Tian; Mei-feng Xiao; et al.", "year": "2019",
  "doi": "10.11777/j.issn1000-3304.2019.19157",
  "abstract": "In this review, we primarily focus on the employment of molecular simulation for unravelling and interpreting the structure-property relationship of PI materials. The molecular chain structures of PI can be finely analyzed in terms of the chains conformation, characteristic ratio and torsion energy barrier. MD simulation or Monte Carlo simulation also works well for understanding the gas separation performance of PI materials through the fractional free volume."},
 {"title": "Electronic structure of polyimide",
  "authors": "J. L. Bredas; T. C. Clarke", "year": "1987",
  "doi": "10.1063/1.452615",
  "abstract": "We present a valence effective Hamiltonian electronic structure calculation on the polyimide PMDA-ODA. The most important result is that the valence electronic bands are calculated to be very narrow. The highest 35 occupied valence bands have widths smaller than 0.25 eV. That result implies that even small disorder effects will tend to strongly localize the electronic processes in polyimide."},
 {"title": "DFT and molecular dynamic simulation for the dielectric property analysis of polyimides",
  "authors": "Huanyu Lei; Xiaolan Li; Junli Wang; et al.", "year": "2022",
  "doi": "10.1016/j.cplett.2021.139131",
  "abstract": "The dielectric properties of polyimides were analyzed by density functional theory calculation for dielectric constant prediction and molecular dynamic simulation for dielectric performance explanation. The best functional/basis set combinations are optimized to be CAM-B3LYP and 6-311++g(d,p). The molecular parameters of band gap, chain mobility, solubility parameter and fractional free volume were simulated and the latter two show high correlation with the dielectric constant."},
 {"title": "Structure-dielectric relationship of polyimides with ultralow dielectric constant and dissipation factors using DFT",
  "authors": "Yu-Che Chen; Yan-Cheng Lin; et al.", "year": "2022",
  "doi": "10.1016/j.polymer.2022.125184",
  "abstract": "The relationship between the chemical structures and the dielectric properties of PIs were comprehensively investigated. By utilizing the density functional theory simulation, we successfully remedied the issues of the additive group contribution method. The Dk and Df values of PIs provided strong correlations with volumetric polarizability and volumetric dipole moment."},
 {"title": "DFT-Based Approach For Dielectric Constant Estimation of Soluble Polyimide Insulators",
  "authors": "Hyunjin Park; Hyuk Choi; Jongseok Kim; et al.", "year": "2024",
  "doi": "10.1021/acs.jpcb.3c07296",
  "abstract": "This study applies density functional theory to predict the dielectric constant of soluble polyimides. Various SPIs containing trifluoromethyl groups in the backbone are successfully synthesized and compared with the DFT-estimated values. The DFT-estimated static dielectric constant of the single-chain model accurately reproduces the corresponding experimental value with at least 80% accuracy."},
 {"title": "Prediction and Interpretability Study of the Glass Transition Temperature of Polyimide Based on ML and MD",
  "authors": "Wenjia Huo; Boyang Liang; Xiang Wu; et al.", "year": "2025",
  "doi": "10.3390/polym17152083",
  "abstract": "A dataset comprising 1261 PIs was assembled. A quantitative structure-property relationship targeting Tg was constructed using nine regression algorithms, with the Categorical Boosting demonstrating the highest accuracy, achieving a coefficient of determination of 0.895 for the test set. SHAP analysis identified the NumRotatableBonds descriptor had a significantly negative impact on Tg."},
 {"title": "Property Prediction and Structural Feature Extraction of Polyimide Materials Based on Machine Learning",
  "authors": "Han Zhang; Haoyuan Li; Hanshen Xin; Jianhua Zhang", "year": "2023",
  "doi": "10.1021/acs.jcim.3c00326",
  "abstract": "We collected 652 polyimide molecular structures and used seven popular machine learning algorithms to predict the glass transition temperature and cut-off wavelength. The root mean square error of the glass transition temperature prediction model was 33.92 degrees C, and the correlation coefficient was 0.861. Eight polyimide materials were synthesized and the experimental characterization values agreed with the predicted values."},
 {"title": "Harnessing machine learning for high-throughput screening of high thermal conductivity polyimides",
  "authors": "Jiale Han; Chunhua Ying; Yue Cao; et al.", "year": "2025",
  "doi": "10.1016/j.nxmate.2024.100420",
  "abstract": "We present a machine learning technique with novel multiscale feature engineering approach to predict and identify high thermal conductivity polyimide efficiently. Multiple machine learning models were trained and validated, demonstrating high predictive accuracy and generalizability. High-throughput screening identified polyimide candidates with thermal conductivity values exceeding 0.4 W/(m K), and these predictions were validated using non-equilibrium molecular dynamics simulation."},
 {"title": "Multi-property prediction and high-throughput screening of polyimides",
  "authors": "Bo Zhang; Xueqing Li; Xinxin Xu; et al.", "year": "2024",
  "doi": "10.1016/j.polymer.2024.127603",
  "abstract": "This study focused on four major categories of properties of polyimide, totaling eleven properties. 176 machine learning models were trained for 11 predictions of properties. A high-throughput virtual screening of nearly 7.6 million polyimides was performed based on the trained model."},
 {"title": "Discovery of multi-functional polyimides through high-throughput screening using explainable machine learning",
  "authors": "Lei Tao; Jinlong He; et al.", "year": "2023",
  "doi": "10.1016/j.cej.2023.142949",
  "abstract": "We first build a comprehensive library of more than 8 million hypothetical polyimides. We then establish multiple ML models for the thermal and mechanical properties of polyimides. Following the proposed ML guidance, we successfully synthesized a novel polyimide."},
 {"title": "Multifunctional polyimide performance prediction based on explainable machine learning",
  "authors": "Suisui Wang; Tianyong Zhang; Han Zhang; et al.", "year": "2025",
  "doi": "10.1002/smo2.70020",
  "abstract": "We established predictive models for glass transition temperature, cut-off wavelength, and coefficient of thermal expansion using various machine learning algorithms. The optimal predictive models demonstrated high accuracy and stability. 135 novel PIs were designed and their key properties were obtained without the need for experimental verification."},
 {"title": "Machine Learning-Enabled Parallel Prediction and Co-Design of Polyimides",
  "authors": "Boyang Liang; Wenjia Huo; Xiang Wu; Rongrong Zheng", "year": "2025",
  "doi": "10.1021/acsomega.5c09078",
  "abstract": "We collected 1499 PI structures from relevant literature with experimentally measured glass transition temperature, dielectric constant, and bandgap width. 90 ML models were established to describe the various properties of PI. MD and QM simulations validated ML predictions showing excellent agreement."},
 {"title": "Gas separation properties of aromatic polyimides with bulky groups",
  "authors": "Cristina Alvarez; Angel E. Lozano; et al.", "year": "2020",
  "doi": "10.1016/j.memsci.2020.117959",
  "abstract": "A series of polyimide membranes with high fractional free volume have been studied. The use of FFV, that corresponds to a probe of zero-radius, cannot predict the behavior of several polymers with similar FFV values but different permeability and selectivity. However, simulation has permitted to correlate accurately permeability and selectivity with the accessible free volume to probes of different radii."},
 {"title": "Molecular modelling of polyimides with intrinsic microporosity",
  "authors": "Kai-Shiun Chang; Kuo-Lun Tung; et al.", "year": "2013",
  "doi": "10.1039/C3RA40196K",
  "abstract": "Molecular dynamics simulations and the Monte Carlo method were adopted to investigate the characteristics of polyimide-derived membranes with intrinsic microporosity. The spiro-centres stiffened the PI-derived membrane, resulting in the formation of a larger and more effective free volume. The increased diffusivity in these PI-derived membranes results in an increase in gas permeability."},
 {"title": "Polyimide and polyimide-derived gas separation membranes: A review",
  "authors": "Chuhan Huang; Chengye Zuo; et al.", "year": "2025",
  "doi": "10.1016/j.advmem.2025.100154",
  "abstract": "PI has been widely regarded as an ideal material for high-performance gas separation membranes. However, practical applications of PI membranes have been limited by challenges such as free volume collapse, physical aging, and high gas transport resistance. These issues are considered addressable through the precise regulation of polymer structures via physical and chemical modification strategies."},
 {"title": "Application of machine learning in polyimide structure design and property regulation",
  "authors": "Wenjia Huo; Haiyue Wang; et al.", "year": "2025",
  "doi": "10.1177/09540083251313522",
  "abstract": "Polyimide is widely used in modern industry due to its excellent properties. With the rapid development of high-throughput computing and data-driven technology, machine learning has become an important method for exploring new materials. Applications of ML in material properties prediction, assisting computational simulation technologies and inverse design are reviewed."},
]

# 1. 创建聚酰亚胺提取模板
c.post("/api/templates", json={"name": "聚酰亚胺提取模板", "fields": ["研究问题", "方法", "主要发现", "结论"]})
templates = c.get("/api/templates").get_json()
tpl_id = templates[-1]["id"]

# 2. 导入文献
for p in PAPERS:
    r = c.post("/api/papers", json=p)
    assert r.status_code == 201, p["title"]

papers = c.get("/api/papers").get_json()

# 3. 逐篇提取证据卡片
total = 0
for p in papers:
    r = c.post("/api/extract", json={"paper_id": p["id"], "template_id": tpl_id})
    total += r.get_json()["created"]

# 4. 核验：多数通过，两张驳回（模拟角色③）
cards = c.get("/api/cards").get_json()
for card in cards:
    action = "reject" if card["id"] % 10 == 0 else "approve"
    c.post(f"/api/cards/{card['id']}/verify", json={"action": action})

verified = c.get("/api/cards?status=verified").get_json()

# 5. 重置为 8 节框架（对应思路图）
for s in c.get("/api/sections").get_json():
    c.delete(f"/api/sections/{s['id']}")
SECTIONS = ["引言", "方法总览", "分子动力学", "第一性原理", "机器学习", "膜分离", "讨论", "结论与展望"]
sec_ids = []
for s in SECTIONS:
    r = c.post("/api/sections", json={"title": s})
    sec_ids.append(r.get_json()["id"])

# 6. 把部分已核验卡片挂到章节
for i, card in enumerate(verified[:16]):
    c.post(f"/api/cards/{card['id']}/assign", json={"section_id": sec_ids[i % len(sec_ids)]})

print(f"papers={len(papers)}  cards={total}  verified={len(verified)}  rejected={len(cards)-len(verified)}  sections={len(sec_ids)}")
print("DB:", db)
