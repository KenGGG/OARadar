from oa_knowledge.markdown_classification import classify_document


def test_appointment_notifications_are_grouped_without_person_names():
    for word in ("任免", "任职", "免职", "任命", "聘任"):
        result = classify_document(f"合成组干〔2026〕1号 关于合成人员{word}的通知")
        assert result.document_type == "任免通知"
        assert not result.needs_review


def test_appointment_policy_is_not_an_appointment_notification():
    result = classify_document("关于印发干部任免管理办法的通知")
    assert result.document_type == "政策制度"


def test_attachment_heading_precedes_mixed_oa_title():
    result = classify_document("任免通知；财务报告", filename="关于合成人员免职的通知.pdf")
    assert result.document_type == "任免通知"
    assert classify_document("任免通知；财务报告").needs_review


def test_later_citations_do_not_classify_an_unknown_document():
    result = classify_document("合成材料", body="无明确标题\n" * 20 + "曾经发布任免通知")
    assert result.needs_review


def test_heading_ocr_spaces_are_normalized():
    result = classify_document("合成材料", body="合 成 组 干\n关于合成人员职务任免的通知\n")
    assert result.document_type == "任免通知"


def test_common_clear_headings_have_document_types():
    examples = {"合成保证合同.pdf": "合同协议", "合成股东决议.pdf": "会议材料",
                "关于合成讲座的通知.pdf": "一般通知", "合成信息〔2026〕第1期.pdf": "刊物学习资料",
                "合成事项汇总表.xlsx": "统计表格", "合成项目法审意见.pdf": "风险合规材料"}
    for filename, expected in examples.items():
        result = classify_document("合成事项", filename=filename)
        assert result.document_type == expected
        assert not result.needs_review


def test_internal_work_bulletin_is_a_business_tag_not_publication():
    result=classify_document('业务一部工作简报第1期')
    assert result.document_type=='工作简报'
