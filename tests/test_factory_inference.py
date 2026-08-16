import unittest

from sage_pycharm_stubgen.factory_inference import _declared_return_reference


class _CommonBase:
    pass


class _Declared(_CommonBase):
    pass


class _OtherDeclared(_CommonBase):
    pass


class _AnnotatedFactory:
    def __call__(self, *args, **kwds) -> _Declared:
        return _Declared()


class _UnionAnnotatedFactory:
    def __call__(self, *args, **kwds) -> "_Declared | _OtherDeclared":
        return _Declared()


class _UnannotatedFactory:
    def __call__(self, *args, **kwds):
        return _Declared()


class DeclaredReturnReferenceTests(unittest.TestCase):
    def test_single_class_annotation_is_preferred(self) -> None:
        reference = _declared_return_reference(_AnnotatedFactory())
        self.assertIsNotNone(reference)
        self.assertTrue(reference.endswith("._Declared"), reference)

    def test_union_annotation_collapses_to_common_base(self) -> None:
        reference = _declared_return_reference(_UnionAnnotatedFactory())
        self.assertIsNotNone(reference)
        self.assertTrue(reference.endswith("._CommonBase"), reference)

    def test_unannotated_factory_falls_back_to_probing(self) -> None:
        self.assertIsNone(_declared_return_reference(_UnannotatedFactory()))
